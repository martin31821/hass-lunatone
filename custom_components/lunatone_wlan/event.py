"""Push button events of DALI-2 input devices."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .hub import InputInstance, LunatoneConfigEntry, LunatoneHub

# Long press repeats arrive every ~160 ms, they are left to the device triggers
EVENT_TYPES = [
    "short_press",
    "double_press",
    "long_press_start",
    "long_press_stop",
    "pressed",
    "released",
    "button_stuck",
    "button_free",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LunatoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an event entity for every push button instance."""
    hub = entry.runtime_data

    @callback
    def add(instances: list[InputInstance]) -> None:
        async_add_entities(
            LunatoneButtonEvent(hub, instance)
            for instance in instances
            if instance.is_button
        )

    @callback
    def add_new(instance: InputInstance) -> None:
        add([instance])

    add(hub.instances())
    entry.async_on_unload(
        async_dispatcher_connect(hass, hub.signal_new_instance, add_new)
    )


class LunatoneButtonEvent(EventEntity):
    """Events of one push button instance."""

    _attr_has_entity_name = True
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(self, hub: LunatoneHub, instance: InputInstance) -> None:
        """Initialize the entity."""
        self._hub = hub
        self._instance = instance
        self._attr_unique_id = (
            f"{hub.unique_id}_cd{instance.address}_i{instance.number}"
        )
        self._attr_translation_placeholders = {"instance": str(instance.number)}
        self._attr_device_info = hub.control_device_info(instance.address)
        self._attr_available = hub.gateway.connected

    @property
    def extra_state_attributes(self) -> dict[str, int | bool | None]:
        """Return how the instance is configured on the bus."""
        return {
            "address": self._instance.address,
            "instance": self._instance.number,
            "instance_enabled": self._instance.enabled,
            "event_scheme": self._instance.event_scheme,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to bus events."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._hub.signal_input_event(
                    self._instance.address, self._instance.number
                ),
                self._handle_event,
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
    def _handle_event(self, event_type: str) -> None:
        if event_type not in EVENT_TYPES:
            return
        self._trigger_event(event_type)
        self.async_write_ha_state()

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self._attr_available = connected
        self.async_write_ha_state()
