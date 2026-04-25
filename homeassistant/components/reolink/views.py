"""Reolink Integration views."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from http import HTTPStatus
import logging
from urllib.parse import urlencode

from aiohttp import ClientError, ClientTimeout, web
from reolink_aio.enums import VodRequestType
from reolink_aio.exceptions import ReolinkError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_source import Unresolvable
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import SSLCipherList

from .util import get_host

_LOGGER = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _parse_single_byte_range(
    range_header: str, content_length: int
) -> tuple[int, int] | None:
    """Parse a single HTTP byte range.

    Only the common single-range forms are supported here:
    - bytes=start-end
    - bytes=start-
    - bytes=-suffix_length
    """
    if not range_header.startswith("bytes="):
        return None

    ranges = [value.strip() for value in range_header.removeprefix("bytes=").split(",")]
    if len(ranges) != 1:
        return None

    start_str, end_str = ranges[0].split("-", 1)
    if start_str and end_str:
        start = int(start_str)
        end = int(end_str)
    elif start_str:
        start = int(start_str)
        end = content_length - 1
    elif end_str:
        suffix_length = int(end_str)
        if suffix_length <= 0:
            return None
        start = max(content_length - suffix_length, 0)
        end = content_length - 1
    else:
        return None

    if start < 0 or end < start or start >= content_length:
        return None

    return start, min(end, content_length - 1)


@callback
def async_generate_playback_proxy_url(
    config_entry_id: str,
    channel: int,
    filename: str,
    stream_res: str,
    vod_type: str,
    file_size: int | None = None,
) -> str:
    """Generate proxy URL for event video."""

    url_format = PlaybackProxyView.url
    url = url_format.format(
        config_entry_id=config_entry_id,
        channel=channel,
        filename=urlsafe_b64encode(filename.encode("utf-8")).decode("utf-8"),
        stream_res=stream_res,
        vod_type=vod_type,
    )
    if file_size is None:
        return url
    return f"{url}?{urlencode({'size': str(file_size)})}"


class PlaybackProxyView(HomeAssistantView):
    """View to proxy playback video from Reolink."""

    requires_auth = True
    url = "/api/reolink/video/{config_entry_id}/{channel}/{stream_res}/{vod_type}/{filename}"
    name = "api:reolink_playback"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize a proxy view."""
        self.hass = hass
        self.session = async_get_clientsession(
            hass,
            verify_ssl=False,
            ssl_cipher=SSLCipherList.INSECURE,
        )
        self._vod_type: str | None = None

    async def head(
        self,
        request: web.Request,
        config_entry_id: str,
        channel: str,
        stream_res: str,
        vod_type: str,
        filename: str,
    ) -> web.StreamResponse:
        """Handle HEAD requests for playback proxy video response."""
        return await self.get(
            request,
            config_entry_id,
            channel,
            stream_res,
            vod_type,
            filename,
        )

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        channel: str,
        stream_res: str,
        vod_type: str,
        filename: str,
        retry: int = 2,
    ) -> web.StreamResponse:
        """Get playback proxy video response."""
        retry = retry - 1

        filename_decoded = urlsafe_b64decode(filename.encode("utf-8")).decode("utf-8")
        ch = int(channel)
        if self._vod_type is not None:
            vod_type = self._vod_type
        try:
            host = get_host(self.hass, config_entry_id)
        except Unresolvable:
            err_str = f"Reolink playback proxy could not find config entry id: {config_entry_id}"
            _LOGGER.warning(err_str)
            return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)

        try:
            _mime_type, reolink_url = await host.api.get_vod_source(
                ch, filename_decoded, stream_res, VodRequestType(vod_type)
            )
        except ReolinkError as err:
            _LOGGER.warning("Reolink playback proxy error: %s", str(err))
            return web.Response(body=str(err), status=HTTPStatus.BAD_REQUEST)

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS | {"host", "referer"}
        }

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Requested Playback Proxy Method %s, Headers: %s",
                request.method,
                headers,
            )
            _LOGGER.debug(
                "Opening VOD stream from %s: %s",
                host.api.camera_name(ch),
                host.api.hide_password(reolink_url),
            )

        try:
            reolink_response = await self.session.get(
                reolink_url,
                headers=headers,
                timeout=ClientTimeout(
                    connect=15, sock_connect=15, sock_read=5, total=None
                ),
            )
        except ClientError as err:
            err_str = host.api.hide_password(
                f"Reolink playback error while getting mp4: {err!s}"
            )
            if retry <= 0:
                _LOGGER.warning(err_str)
                return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)
            _LOGGER.debug("%s, renewing token", err_str)
            await host.api.expire_session(unsubscribe=False)
            return await self.get(
                request, config_entry_id, channel, stream_res, vod_type, filename, retry
            )

        # Reolink typo "apolication/octet-stream" instead of "application/octet-stream"
        if reolink_response.content_type not in [
            "video/mp4",
            "application/octet-stream",
            "apolication/octet-stream",
        ]:
            err_str = f"Reolink playback expected video/mp4 but got {reolink_response.content_type}"
            if (
                reolink_response.content_type == "video/x-flv"
                and vod_type == VodRequestType.PLAYBACK.value
            ):
                # next time use DOWNLOAD immediately
                self._vod_type = VodRequestType.DOWNLOAD.value
                _LOGGER.debug(
                    "%s, retrying using download instead of playback cmd", err_str
                )
                return await self.get(
                    request,
                    config_entry_id,
                    channel,
                    stream_res,
                    self._vod_type,
                    filename,
                    retry,
                )

            _LOGGER.error(err_str)
            if reolink_response.content_type == "text/html":
                text = await reolink_response.text()
                _LOGGER.debug(text)
            return web.Response(body=err_str, status=HTTPStatus.BAD_REQUEST)

        total_content_length = reolink_response.content_length
        if total_content_length is None and (size_param := request.query.get("size")):
            try:
                parsed_size = int(size_param)
            except ValueError:
                parsed_size = 0
            if parsed_size > 0:
                total_content_length = parsed_size

        # Safari/iOS range probing is sensitive to unknown-length chunked responses.
        # If playback returns a ranged request as 200 without content length, retry
        # once using DOWNLOAD which is more likely to expose deterministic byte ranges.
        if (
            request.headers.get("Range") is not None
            and reolink_response.status == HTTPStatus.OK
            and total_content_length is None
            and vod_type == VodRequestType.PLAYBACK.value
            and retry > 0
        ):
            _LOGGER.debug(
                "Playback range request for %s returned unknown content length; retrying with download cmd",
                host.api.camera_name(ch),
            )
            reolink_response.release()
            return await self.get(
                request,
                config_entry_id,
                channel,
                stream_res,
                VodRequestType.DOWNLOAD.value,
                filename,
                retry,
            )

        response_headers = {
            key: value
            for key, value in reolink_response.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        }
        _LOGGER.debug(
            "Response Playback Proxy Status %s:%s, Headers: %s",
            reolink_response.status,
            reolink_response.reason,
            response_headers,
        )

        content_type = response_headers.get(
            "Content-Type", reolink_response.content_type
        )
        if content_type == "apolication/octet-stream":
            content_type = "application/octet-stream"
        if content_type == "application/octet-stream":
            content_type = "video/mp4"

        response_headers["Content-Type"] = content_type
        response_headers.setdefault("Accept-Ranges", "bytes")

        _LOGGER.info(
            "Starting Reolink playback stream for %s channel %s (%s, %s)",
            host.api.camera_name(ch),
            ch,
            stream_res,
            filename_decoded,
        )

        if (
            total_content_length is not None
            and "Content-Length" not in response_headers
        ):
            response_headers["Content-Length"] = str(total_content_length)

        range_header = request.headers.get("Range")
        range_bounds: tuple[int, int] | None = None
        if (
            range_header is not None
            and total_content_length is not None
            and reolink_response.status != HTTPStatus.PARTIAL_CONTENT
        ):
            range_bounds = _parse_single_byte_range(range_header, total_content_length)
            if range_bounds is not None:
                range_start, range_end = range_bounds
                response_headers["Content-Range"] = (
                    f"bytes {range_start}-{range_end}/{total_content_length}"
                )
                response_headers["Content-Length"] = str(range_end - range_start + 1)

        response_status = reolink_response.status
        response_reason = reolink_response.reason
        if range_bounds is not None and response_status == HTTPStatus.OK:
            response_status = HTTPStatus.PARTIAL_CONTENT
            response_reason = HTTPStatus.PARTIAL_CONTENT.phrase

        response = web.StreamResponse(
            status=response_status,
            reason=response_reason,
            headers=response_headers,
        )

        await response.prepare(request)

        if request.method == "HEAD":
            reolink_response.release()
            await response.write_eof()
            return response

        try:
            if range_bounds is None:
                async for chunk in reolink_response.content.iter_chunked(65536):
                    await response.write(chunk)
            else:
                range_start, range_end = range_bounds
                offset = 0
                remaining = range_end - range_start + 1
                async for chunk in reolink_response.content.iter_chunked(65536):
                    chunk_end = offset + len(chunk)
                    if chunk_end <= range_start:
                        offset = chunk_end
                        continue

                    slice_start = max(range_start - offset, 0)
                    slice_end = min(slice_start + remaining, len(chunk))
                    if slice_start < slice_end:
                        await response.write(chunk[slice_start:slice_end])
                        remaining -= slice_end - slice_start
                        if remaining <= 0:
                            break

                    offset = chunk_end
        except TimeoutError:
            _LOGGER.debug(
                "Timeout while reading Reolink playback from %s, writing EOF",
                host.api.nvr_name,
            )
        finally:
            reolink_response.release()

        await response.write_eof()
        return response
