"""Illuminance of DALI-2 light sensors."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import LIGHT_LUX
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import LunatoneInstanceEntity
from .hub import InputInstance, LunatoneConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LunatoneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an illuminance sensor for every light sensor instance."""
    hub = entry.runtime_data

    @callback
    def add(instances: list[InputInstance]) -> None:
        async_add_entities(
            LunatoneIlluminanceSensor(hub, instance, "illuminance")
            for instance in instances
            if instance.is_light
        )

    @callback
    def add_new(instance: InputInstance) -> None:
        add([instance])

    add(hub.instances())
    entry.async_on_unload(
        async_dispatcher_connect(hass, hub.signal_new_instance, add_new)
    )


class LunatoneIlluminanceSensor(LunatoneInstanceEntity, SensorEntity):
    """Illuminance measured by a light sensor instance.

    IEC 62386-304 values are relative. Sensors like the Lunatone CS Mini
    report 1 lx per step at full resolution, so the value is shown in lux.
    """

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        """Return the last reported illuminance."""
        value = self._value
        return value if isinstance(value, int) else None
