"""Gateway buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .gateway import GatewayError
from .hub import LunatoneConfigEntry, LunatoneHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LunatoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the scan button."""
    async_add_entities([LunatoneScanButton(entry.runtime_data)])


class LunatoneScanButton(ButtonEntity):
    """Scan the bus for control devices."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "scan_bus"

    def __init__(self, hub: LunatoneHub) -> None:
        """Initialize the button."""
        self._hub = hub
        self._attr_unique_id = f"{hub.unique_id}_scan_bus"
        self._attr_device_info = hub.gateway_device_info
        self._attr_available = hub.gateway.connected

    async def async_added_to_hass(self) -> None:
        """Track the gateway connection."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_connection, self._handle_connection
            )
        )

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self._attr_available = connected
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Scan the bus."""
        try:
            await self._hub.async_scan()
        except GatewayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_failed",
                translation_placeholders={"error": str(err)},
            ) from err
