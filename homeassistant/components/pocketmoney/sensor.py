"""Sensor for next pocket money day."""

from __future__ import annotations

from datetime import date, timedelta
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, SENSOR_NEXT_DAY


def _last_sunday(year: int, month: int) -> date:
    """Return the date of the last Sunday of the given month."""
    # Start from last day of month, go backwards to Sunday
    last_day = date(year, month, 1)
    if month == 12:
        last_day = last_day.replace(month=12, day=31)
    else:
        last_day = last_day.replace(month=month + 1, day=1) - timedelta(days=1)
    while last_day.weekday() != 6:  # 6 = Sunday
        last_day -= timedelta(days=1)
    return last_day


def get_next_pocketmoney_day(today: date) -> date:
    """Return the next pocket money day (last Sunday of this or next month)."""
    this_month = _last_sunday(today.year, today.month)
    if today <= this_month:
        return this_month
    # Otherwise, next month
    next_month = today.month + 1
    next_year = today.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    return _last_sunday(next_year, next_month)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the pocket money sensor."""
    async_add_entities([PocketMoneyDaySensor(entry)])


class PocketMoneyDaySensor(SensorEntity):
    """Sensor for next pocket money day."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_pocketmoney_day"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_unique_id = SENSOR_NEXT_DAY

    def __init__(self, entry):
        """Initialize the sensor."""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "pocketmoney")},
            name="Pocket Money",
            manufacturer="PocketMoney",
            model="PocketMoney",
        )
        self._attr_name = None  # Use translation key

    @property
    def native_value(self):
        """Return the next pocket money day as ISO date."""
        today = date.today()
        return get_next_pocketmoney_day(today).isoformat()
