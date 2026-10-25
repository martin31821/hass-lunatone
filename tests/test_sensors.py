"""Tests for occupancy and light sensors."""

from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_get_device_automations,
)

from custom_components.lunatone_wlan.const import (
    DOMAIN,
    EVENT_ILLUMINANCE,
    EVENT_OCCUPANCY,
)
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import SERIAL, get_device
from .fake_gateway import FakeGateway, FakeInstance, cs_mini

OCCUPANCY = "binary_sensor.dali_input_device_a5_occupancy"
MOVEMENT = "binary_sensor.dali_input_device_a5_movement"
ILLUMINANCE = "sensor.dali_input_device_a5_illuminance"


async def _wait_for(hass: HomeAssistant, condition, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()


async def _push(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    fake_gateway: FakeGateway,
    frame: list[int],
) -> None:
    await fake_gateway.push_monitor(frame)
    await entry.runtime_data.gateway.send_frame([0xFF, 0xFE, 0x00])
    await hass.async_block_till_done()


async def test_sensor_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The CS Mini gets occupancy, movement and illuminance with current values."""
    assert hass.states.get(OCCUPANCY).state == STATE_OFF
    assert hass.states.get(OCCUPANCY).attributes["device_class"] == "occupancy"
    assert hass.states.get(MOVEMENT).state == STATE_OFF
    assert hass.states.get(MOVEMENT).attributes["device_class"] == "motion"
    illuminance = hass.states.get(ILLUMINANCE)
    assert illuminance.state == "40"
    assert illuminance.attributes["unit_of_measurement"] == "lx"
    assert illuminance.attributes["device_class"] == "illuminance"
    device = get_device(device_registry, f"{SERIAL}_cd5")
    assert device.model == "Light sensor + Occupancy sensor"
    assert get_device(device_registry, f"{SERIAL}_cd0").model == "Push button"


async def test_occupancy_events(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Occupancy events from the CS Mini update both binary sensors."""
    events = async_capture_events(hass, EVENT_OCCUPANCY)

    await _push(hass, setup_integration, fake_gateway, [0x0A, 0x80, 0x0B])
    assert hass.states.get(OCCUPANCY).state == STATE_ON
    assert hass.states.get(MOVEMENT).state == STATE_ON

    await _push(hass, setup_integration, fake_gateway, [0x0A, 0x80, 0x0A])
    assert hass.states.get(OCCUPANCY).state == STATE_ON
    assert hass.states.get(MOVEMENT).state == STATE_OFF

    await _push(hass, setup_integration, fake_gateway, [0x0A, 0x80, 0x08])
    assert hass.states.get(OCCUPANCY).state == STATE_OFF

    assert [(e.data["occupied"], e.data["movement"]) for e in events] == [
        (True, True),
        (True, False),
        (False, False),
    ]
    assert events[0].data["address"] == 5
    assert events[0].data["instance"] == 0
    assert events[0].data["device_id"] is not None


async def test_light_events(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Light events are converted to lux."""
    events = async_capture_events(hass, EVENT_ILLUMINANCE)
    await _push(hass, setup_integration, fake_gateway, [0x0A, 0x84, 0x13])
    assert hass.states.get(ILLUMINANCE).state == "38"
    # 10-bit event information above 255
    await _push(hass, setup_integration, fake_gateway, [0x0A, 0x85, 0x2A])
    assert hass.states.get(ILLUMINANCE).state == "596"
    assert [e.data["illuminance"] for e in events] == [38, 596]


async def test_values_refreshed_after_reconnect(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Sensor values are read again after the connection comes back."""
    fake_gateway.devices[5] = cs_mini(lux=215, occupancy=0b11)
    await fake_gateway.disconnect_clients()
    await _wait_for(hass, lambda: hass.states.get(ILLUMINANCE).state == "215", 5)
    assert hass.states.get(MOVEMENT).state == STATE_ON


async def test_readdressed_device(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """When a scan finds a sensor where buttons were, entities are replaced."""
    assert hass.states.get("event.dali_input_device_a1_button_3") is not None
    fake_gateway.devices[1] = cs_mini(lux=12)
    await hass.services.async_call(DOMAIN, "scan_bus", {}, blocking=True)
    await hass.async_block_till_done()

    for number in range(4):
        assert hass.states.get(f"event.dali_input_device_a1_button_{number}") is None
        assert not entity_registry.async_get_entity_id(
            "event", DOMAIN, f"{SERIAL}_cd1_i{number}"
        )
    assert hass.states.get("binary_sensor.dali_input_device_a1_occupancy") is not None
    assert hass.states.get("sensor.dali_input_device_a1_illuminance").state == "12"
    assert set(setup_integration.runtime_data.devices[1].instances) == {0, 1}


async def test_numbered_names(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Devices with several sensors of one type get numbered names."""
    fake_gateway.devices[7] = [
        FakeInstance(instance_type=4, resolution=11, value=1),
        FakeInstance(instance_type=4, resolution=11, value=2),
    ]
    await hass.services.async_call(DOMAIN, "scan_bus", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dali_input_device_a7_illuminance_0").state == "1"
    assert hass.states.get("sensor.dali_input_device_a7_illuminance_1").state == "2"


async def test_unknown_sensor_identified(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A light event from an unscanned device is not mistaken for a button."""
    fake_gateway.devices[6] = [FakeInstance(instance_type=4, resolution=11, value=0)]
    events = async_capture_events(hass, EVENT_ILLUMINANCE)
    await _push(hass, setup_integration, fake_gateway, [0x0C, 0x80, 0x05])
    await _wait_for(hass, lambda: events)
    assert events[0].data["illuminance"] == 10
    assert hass.states.get("sensor.dali_input_device_a6_illuminance").state == "10"
    assert hass.states.get("event.dali_input_device_a6_button_0") is None


async def test_sensor_device_triggers(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Sensor devices get the standard occupancy and illuminance triggers."""
    device = get_device(device_registry, f"{SERIAL}_cd5")
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    types = {(t["domain"], t["type"]) for t in triggers}
    assert ("binary_sensor", "occupied") in types
    assert ("binary_sensor", "not_occupied") in types
    assert ("binary_sensor", "motion") in types
    assert ("sensor", "illuminance") in types
    assert not [t for t in triggers if t["domain"] == DOMAIN]
