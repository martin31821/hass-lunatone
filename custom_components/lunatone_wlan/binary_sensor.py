"""Occupancy and movement of DALI-2 occupancy sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .dali import OccupancyState
from .entity import LunatoneInstanceEntity
from .hub import InputInstance, LunatoneConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LunatoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add occupancy and movement sensors for every occupancy instance."""
    hub = entry.runtime_data

    @callback
    def add(instances: list[InputInstance]) -> None:
        async_add_entities(
            entity
            for instance in instances
            if instance.is_occupancy
            for entity in (
                LunatoneOccupancySensor(hub, instance, "occupancy"),
                LunatoneOccupancySensor(hub, instance, "movement"),
            )
        )

    @callback
    def add_new(instance: InputInstance) -> None:
        add([instance])

    add(hub.instances())
    entry.async_on_unload(
        async_dispatcher_connect(hass, hub.signal_new_instance, add_new)
    )


class LunatoneOccupancySensor(LunatoneInstanceEntity, BinarySensorEntity):
    """Occupied or movement state of an occupancy sensor instance."""

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return occupancy or motion."""
        if self._key == "occupancy":
            return BinarySensorDeviceClass.OCCUPANCY
        return BinarySensorDeviceClass.MOTION

    @property
    def is_on(self) -> bool | None:
        """Return True while occupied or while movement is detected."""
        if not isinstance(state := self._value, OccupancyState):
            return None
        if self._key == "occupancy":
            return state.occupied
        return state.movement
