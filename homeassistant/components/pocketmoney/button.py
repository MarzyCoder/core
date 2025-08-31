"""Button for giving pocket money."""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import BUTTON_GIVE, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the pocket money button."""
    async_add_entities([GivePocketMoneyButton(entry)])


class GivePocketMoneyButton(ButtonEntity):
    """Button to give pocket money."""

    _attr_has_entity_name = True
    _attr_translation_key = "give_pocket_money"
    _attr_unique_id = BUTTON_GIVE

    def __init__(self, entry):
        """Initialize the button."""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "pocketmoney")},
            name="Pocket Money",
            manufacturer="PocketMoney",
            model="PocketMoney",
        )
        self._attr_name = None  # Use translation key

    async def async_press(self) -> None:
        """Handle the button press."""
        # For now, just log or update a timestamp attribute
        self._attr_extra_state_attributes = {
            "last_given": self.hass.helpers.event.dt_util.utcnow().isoformat()
        }
        self.async_write_ha_state()
