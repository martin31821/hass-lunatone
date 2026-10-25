"""Base entity for DALI-2 input device instances."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .hub import InputInstance, LunatoneHub


class LunatoneInstanceEntity(Entity):
    """An entity fed by the events of one input instance."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, hub: LunatoneHub, instance: InputInstance, key: str | None = None
    ) -> None:
        """Initialize the entity.

        Devices with several instances of the same type get numbered names.
        """
        self._hub = hub
        self._instance = instance
        self._key = key
        unique_id = hub.instance_unique_id(instance)
        self._attr_unique_id = f"{unique_id}_{key}" if key else unique_id
        self._attr_device_info = hub.control_device_info(instance.address)
        self._attr_available = hub.gateway.connected
        self._attr_translation_placeholders = {"instance": str(instance.number)}
        device = hub.devices[instance.address]
        if key and device.count(instance.instance_type) > 1:
            self._attr_translation_key = f"{key}_numbered"
        elif key:
            self._attr_translation_key = key

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how the instance is configured on the bus."""
        return {
            "address": self._instance.address,
            "instance": self._instance.number,
            "instance_enabled": self._instance.enabled,
            "event_scheme": self._instance.event_scheme,
        }

    @property
    def _value(self) -> Any:
        return self._hub.values.get((self._instance.address, self._instance.number))

    async def async_added_to_hass(self) -> None:
        """Subscribe to bus events and the gateway connection."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._hub.signal_input_event(
                    self._instance.address, self._instance.number
                ),
                self._handle_value,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_connection, self._handle_connection
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal_updated, self.async_write_ha_state
            )
        )

    @callback
    def _handle_value(self, value: Any) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self._attr_available = connected
        self.async_write_ha_state()
