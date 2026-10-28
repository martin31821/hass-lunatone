"""Tests for setup, bus scan and bus events."""

from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.lunatone_wlan.const import DOMAIN, EVENT_BUTTON, EVENT_MONITOR
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import SERIAL, get_device
from .fake_gateway import FakeGateway, FakeInstance


async def _push(
    hass: HomeAssistant, fake_gateway: FakeGateway, frame: list[int]
) -> None:
    await fake_gateway.push_monitor(frame)
    # Round trip a frame so the pushed one has been read
    await config_hub(hass).gateway.send_frame([0xFF, 0xFE, 0x00])
    await hass.async_block_till_done()


async def _wait_for(hass: HomeAssistant, condition, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()


def config_hub(hass: HomeAssistant):
    """Return the hub of the only config entry."""
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


async def test_setup_scans_bus(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The first setup finds the control devices and their buttons."""
    assert setup_integration.state is ConfigEntryState.LOADED

    gateway = get_device(device_registry, SERIAL)
    assert gateway is not None
    assert gateway.name == "Lunatone Dali WLAN"
    assert gateway.serial_number == SERIAL
    assert gateway.sw_version == "1.1.2/1.0.10"

    for address in range(4):
        device = get_device(device_registry, f"{SERIAL}_cd{address}")
        assert device is not None
        assert device.name == f"DALI input device A{address}"
        assert device.via_device_id == gateway.id

    entity_id = entity_registry.async_get_entity_id("event", DOMAIN, f"{SERIAL}_cd3_i1")
    assert entity_id == "event.dali_input_device_a3_button_1"
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["instance_enabled"] is False
    assert state.attributes["event_scheme"] == 0
    assert (
        len(
            er.async_entries_for_config_entry(
                entity_registry, setup_integration.entry_id
            )
        )
        == 25
    )


async def test_setup_not_ready(hass: HomeAssistant) -> None:
    """Setup is retried while the gateway is unreachable."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SERIAL, data={CONF_HOST: "127.0.0.1:1"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_restart_restores_devices(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Known devices are restored before the bus is scanned again."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    fake_gateway.respond = False
    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("event.dali_input_device_a0_button_3") is not None
    assert hass.states.get("binary_sensor.dali_input_device_a5_occupancy") is not None
    fake_gateway.respond = True
    await setup_integration.runtime_data.initial_scan


async def test_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Unloading closes the connection."""
    hub = setup_integration.runtime_data
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert not hub.gateway.connected


async def test_button_event(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A push button event updates the event entity and fires a bus event."""
    buttons = async_capture_events(hass, EVENT_BUTTON)
    monitor = async_capture_events(hass, EVENT_MONITOR)

    # A2 instance 3 double press, as captured
    await _push(hass, fake_gateway, [0x04, 0x8C, 0x05])

    state = hass.states.get("event.dali_input_device_a2_button_3")
    assert state.attributes["event_type"] == "double_press"
    device = get_device(device_registry, f"{SERIAL}_cd2")
    assert buttons[0].data == {
        "gateway": SERIAL,
        "device_id": device.id,
        "address": 2,
        "instance": 3,
        "type": "double_press",
        "scheme": "device_instance",
    }
    assert monitor[0].data == {
        "gateway": SERIAL,
        "bits": 24,
        "data": [4, 140, 5],
        "hex": "048C05",
        "frame": "event",
        "scheme": "device_instance",
        "info": 5,
        "short_address": 2,
        "instance_number": 3,
    }


async def test_long_press_repeat_only_on_bus(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Long press repeats are bus events but do not touch the entity."""
    buttons = async_capture_events(hass, EVENT_BUTTON)
    await _push(hass, fake_gateway, [0x06, 0x80, 0x0B])
    assert buttons[0].data["type"] == "long_press_repeat"
    assert hass.states.get("event.dali_input_device_a3_button_0").state == STATE_UNKNOWN


async def test_scene_command_from_button(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Direct scene commands of the MC2 show up as monitor events."""
    monitor = async_capture_events(hass, EVENT_MONITOR)
    await _push(hass, fake_gateway, [141, 18])
    assert monitor[0].data == {
        "gateway": SERIAL,
        "bits": 16,
        "data": [141, 18],
        "hex": "8D12",
        "frame": "command",
        "target": "group",
        "group": 6,
        "opcode": 18,
        "scene": 2,
    }


async def test_unknown_device_is_added(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Events from unknown devices are identified and added."""
    fake_gateway.devices[10] = [FakeInstance(), FakeInstance()]
    buttons = async_capture_events(hass, EVENT_BUTTON)
    await _push(hass, fake_gateway, [0x14, 0x84, 0x02])
    await _wait_for(hass, lambda: buttons)
    assert buttons[0].data["address"] == 10
    assert buttons[0].data["type"] == "short_press"
    assert get_device(device_registry, f"{SERIAL}_cd10")
    state = hass.states.get("event.dali_input_device_a10_button_1")
    assert state is not None


async def test_instance_scheme_event_resolved(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_gateway: FakeGateway
) -> None:
    """Instance scheme events map to the only enabled matching instance."""
    hub = setup_integration.runtime_data
    for instance in hub.instances():
        instance.enabled = instance.address == 3 and instance.number == 0
    buttons = async_capture_events(hass, EVENT_BUTTON)
    await _push(hass, fake_gateway, [0x82, 0x80, 0x02])
    assert buttons[0].data["address"] == 3
    assert buttons[0].data["instance"] == 0
    assert (
        hass.states.get("event.dali_input_device_a3_button_0").attributes["event_type"]
        == "short_press"
    )


async def test_instance_scheme_event_ambiguous(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_gateway: FakeGateway
) -> None:
    """Ambiguous instance scheme events are fired without device."""
    buttons = async_capture_events(hass, EVENT_BUTTON)
    await _push(hass, fake_gateway, [0x82, 0x80, 0x02])
    assert buttons[0].data["device_id"] is None
    assert buttons[0].data["address"] is None
    assert buttons[0].data["instance"] == 0


async def test_unavailable_while_disconnected(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Entities are unavailable while the gateway is disconnected."""
    fake_gateway.respond = True
    await fake_gateway.stop()
    await hass.async_block_till_done()
    assert (
        hass.states.get("event.dali_input_device_a3_button_0").state
        == STATE_UNAVAILABLE
    )


async def test_remove_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Control devices can be removed, the gateway cannot."""
    assert await async_setup_component(hass, "config", {})
    hub = setup_integration.runtime_data
    device = get_device(device_registry, f"{SERIAL}_cd1")
    gateway = get_device(device_registry, SERIAL)
    from custom_components.lunatone_wlan import async_remove_config_entry_device

    assert not await async_remove_config_entry_device(hass, setup_integration, gateway)
    assert await async_remove_config_entry_device(hass, setup_integration, device)
    assert 1 not in hub.devices


def _event_types(events: list[Event]) -> list[str]:
    return [event.data["type"] for event in events]
