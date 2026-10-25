"""Push button events of DALI-2 input devices."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import LunatoneInstanceEntity
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


class LunatoneButtonEvent(LunatoneInstanceEntity, EventEntity):
    """Events of one push button instance."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "button"

    def __init__(self, hub: LunatoneHub, instance: InputInstance) -> None:
        """Initialize the entity."""
        super().__init__(hub, instance)

    @callback
    def _handle_value(self, value: Any) -> None:
        if value not in EVENT_TYPES:
            return
        self._trigger_event(value)
        self.async_write_ha_state()
