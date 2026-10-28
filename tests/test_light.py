"""Tests for control gear lights."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.lunatone_wlan import async_remove_config_entry_device
from custom_components.lunatone_wlan.const import DOMAIN
from custom_components.lunatone_wlan.hub import GEAR_POLL_INTERVAL
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import SERIAL, get_device
from .fake_gateway import FakeGateway, FakeGear

LIGHT_A0 = "light.dali_light_a0"
LIGHT_A1 = "light.dali_light_a1"
LIGHT_A16 = "light.dali_light_a16"
GROUP_0 = "light.dali_group_0"
GROUP_5 = "light.dali_group_5"


@pytest.fixture(autouse=True)
def fast_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read gear levels without waiting."""
    monkeypatch.setattr("custom_components.lunatone_wlan.hub.GEAR_REFRESH_DELAY", 0)
    monkeypatch.setattr("custom_components.lunatone_wlan.hub.FADE_POLL_INTERVAL", 0)


async def _wait_for(hass: HomeAssistant, condition, timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)
            await hass.async_block_till_done()


async def _bus_command(
    hass: HomeAssistant, fake_gateway: FakeGateway, frame: list[int]
) -> None:
    """Send a frame from another bus participant, e.g. an MC2."""
    fake_gateway.apply(frame)
    await fake_gateway.push_monitor(frame)


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        "light", service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


async def test_lights_found(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Every control gear gets a device and a dimmable light."""
    gateway = get_device(device_registry, SERIAL)
    device = get_device(device_registry, f"{SERIAL}_cg0")
    assert device.name == "DALI light A0"
    assert device.model == "Incandescent lamp dimmer"
    assert device.via_device_id == gateway.id
    assert (
        get_device(device_registry, f"{SERIAL}_cg16").model
        == "LED module + Colour control"
    )
    assert (
        entity_registry.async_get_entity_id("light", DOMAIN, f"{SERIAL}_cg1")
        == LIGHT_A1
    )

    state = hass.states.get(LIGHT_A0)
    assert state.state == STATE_OFF
    assert state.attributes["supported_color_modes"] == [ColorMode.BRIGHTNESS]
    assert state.attributes["address"] == 0
    assert state.attributes["groups"] == [0, 2]
    assert state.attributes["lamp_failure"] is False
    assert hass.states.get(LIGHT_A16).attributes["lamp_failure"] is True


async def test_turn_on_brightness(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Brightness is sent as direct arc power."""
    await _call(hass, "turn_on", LIGHT_A0, **{ATTR_BRIGHTNESS: 128})
    assert fake_gateway.frames[-1] == [0, 127]
    state = hass.states.get(LIGHT_A0)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    await _wait_for(hass, lambda: fake_gateway.frames[-1] == [1, 0xA0])

    await _call(hass, "turn_on", LIGHT_A0, **{ATTR_BRIGHTNESS: 1})
    assert fake_gateway.frames[-1] == [0, 1]


async def test_turn_on_clamps_to_max_level(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Without a previous level the gear goes to max, which may be below 254."""
    await _call(hass, "turn_on", LIGHT_A1)
    assert fake_gateway.frames[-1] == [3, 0x05]
    assert hass.states.get(LIGHT_A1).attributes[ATTR_BRIGHTNESS] == 247

    await _call(hass, "turn_on", LIGHT_A1, **{ATTR_BRIGHTNESS: 255})
    assert fake_gateway.frames[-1] == [2, 246]


async def test_turn_off_and_back_on(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Turning on without brightness restores the last level."""
    await _call(hass, "turn_on", LIGHT_A0, **{ATTR_BRIGHTNESS: 100})
    await _call(hass, "turn_off", LIGHT_A0)
    assert fake_gateway.frames[-1] == [1, 0x00]
    assert hass.states.get(LIGHT_A0).state == STATE_OFF

    await _call(hass, "turn_on", LIGHT_A0)
    assert fake_gateway.frames[-1] == [0, 100]
    assert fake_gateway.gear[0].level == 100


async def test_send_failure(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A rejected frame raises and keeps the state."""
    fake_gateway.ack_result = 1
    with pytest.raises(HomeAssistantError, match="rejected"):
        await _call(hass, "turn_on", LIGHT_A0)
    assert hass.states.get(LIGHT_A0).state == STATE_OFF


async def test_group_scene_from_bus(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A scene sent to a group by a button updates the lights in that group."""
    fake_gateway.received.clear()
    await _bus_command(hass, fake_gateway, [0x81, 0x11])
    await _wait_for(hass, lambda: hass.states.get(LIGHT_A1).state == STATE_ON)
    assert hass.states.get(LIGHT_A0).attributes["level"] == 40
    assert hass.states.get(LIGHT_A1).attributes["level"] == 80
    assert hass.states.get(LIGHT_A16).state == STATE_OFF
    # Gear outside group 0 is not queried
    assert not [frame for frame in fake_gateway.frames if frame[0] >> 1 == 16]


async def test_broadcast_off_from_bus(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Broadcast commands update every light."""
    await _call(hass, "turn_on", LIGHT_A0)
    await _call(hass, "turn_on", LIGHT_A1)
    await _bus_command(hass, fake_gateway, [0xFF, 0x00])
    await _wait_for(
        hass,
        lambda: (
            hass.states.get(LIGHT_A0).state == STATE_OFF
            and hass.states.get(LIGHT_A1).state == STATE_OFF
        ),
    )


async def test_level_shown_after_fade(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Levels are read again until the fade has finished."""
    fake_gateway.gear[0].fade_queries = 3
    await _bus_command(hass, fake_gateway, [0x01, 0x10])
    await _wait_for(hass, lambda: hass.states.get(LIGHT_A0).state == STATE_ON)
    assert fake_gateway.gear[0].fade_queries == 0
    assert hass.states.get(LIGHT_A0).attributes["level"] == 254


async def test_queries_do_not_trigger_refresh(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Queries seen on the bus, including our own, are not level changes."""
    fake_gateway.received.clear()
    await fake_gateway.push_monitor([0x01, 0xA0])
    await setup_integration.runtime_data.gateway.send_frame([0xFF, 0xFE, 0x00])
    await hass.async_block_till_done()
    assert fake_gateway.frames == [[0xFF, 0xFE, 0x00]]


async def test_poll_marks_missing_gear_unavailable(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Gear switched off at the mains becomes unavailable until it answers."""
    fake_gateway.gear[1].present = False
    async_fire_time_changed(hass, dt_util.utcnow() + GEAR_POLL_INTERVAL)
    await _wait_for(hass, lambda: hass.states.get(LIGHT_A1).state == STATE_UNAVAILABLE)

    fake_gateway.gear[1].present = True
    fake_gateway.gear[1].level = 254
    async_fire_time_changed(hass, dt_util.utcnow() + 2 * GEAR_POLL_INTERVAL)
    await _wait_for(hass, lambda: hass.states.get(LIGHT_A1).state == STATE_ON)


async def test_rescan_adds_and_keeps_gear(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """New gear is added, gear that does not answer is kept."""
    fake_gateway.gear[17] = FakeGear(device_types=[7], level=254, groups=1 << 6)
    fake_gateway.gear[16].present = False
    response = await hass.services.async_call(
        DOMAIN, "scan_bus", {}, blocking=True, return_response=True
    )
    assert response["control_gear"]["A17"] == {
        "device_types": [7],
        "min_level": 1,
        "max_level": 254,
        "groups": 64,
    }
    assert "A16" not in response["control_gear"]
    await hass.async_block_till_done()

    relay = hass.states.get("light.dali_light_a17")
    assert relay.state == STATE_ON
    assert relay.attributes["supported_color_modes"] == [ColorMode.ONOFF]
    assert hass.states.get(LIGHT_A16).state == STATE_UNAVAILABLE


async def test_restart_restores_lights(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Known gear gets its lights before the bus is scanned again."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    fake_gateway.respond = False
    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT_A16) is not None
    fake_gateway.respond = True
    await setup_integration.runtime_data.initial_scan


async def test_remove_gear(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Control gear can be removed, it comes back with the next scan."""
    device = get_device(device_registry, f"{SERIAL}_cg16")
    assert await async_remove_config_entry_device(hass, setup_integration, device)
    assert 16 not in setup_integration.runtime_data.gear


async def test_group_lights_found(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Every group with gear gets a device and a light."""
    for group in (0, 2, 3, 5):
        device = get_device(device_registry, f"{SERIAL}_group{group}")
        assert device.name == f"DALI group {group}"
        assert device.via_device_id == get_device(device_registry, SERIAL).id
    assert get_device(device_registry, f"{SERIAL}_group1") is None

    state = hass.states.get(GROUP_0)
    assert state.state == STATE_OFF
    assert state.attributes["group"] == 0
    assert state.attributes["entity_id"] == [LIGHT_A0, LIGHT_A1]


async def test_group_commands(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A group is switched with one group addressed frame."""
    await _call(hass, "turn_on", GROUP_0)
    assert fake_gateway.frames[-1] == [0x81, 0x05]
    assert hass.states.get(LIGHT_A0).attributes["level"] == 254
    assert hass.states.get(LIGHT_A1).attributes["level"] == 246
    assert hass.states.get(GROUP_0).attributes["level"] == 250
    assert hass.states.get(LIGHT_A16).state == STATE_OFF

    await _call(hass, "turn_on", GROUP_0, **{ATTR_BRIGHTNESS: 128})
    assert fake_gateway.frames[-1] == [0x80, 127]
    group = hass.states.get(GROUP_0)
    assert group.state == STATE_ON
    assert group.attributes[ATTR_BRIGHTNESS] == 128

    await _call(hass, "turn_off", GROUP_0)
    assert fake_gateway.frames[-1] == [0x81, 0x00]
    assert hass.states.get(GROUP_0).state == STATE_OFF
    assert hass.states.get(LIGHT_A1).state == STATE_OFF

    await _call(hass, "turn_on", GROUP_0)
    assert fake_gateway.frames[-1] == [0x80, 127]
    await _wait_for(hass, lambda: fake_gateway.frames[-1] == [3, 0xA0])
    assert fake_gateway.gear[1].level == 127


async def test_group_follows_members(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """The group is on while any member is on, with their mean level."""
    await _bus_command(hass, fake_gateway, [0x81, 0x11])
    await _wait_for(hass, lambda: hass.states.get(GROUP_0).state == STATE_ON)
    assert hass.states.get(GROUP_0).attributes["level"] == 60

    await _call(hass, "turn_off", LIGHT_A0)
    assert hass.states.get(GROUP_0).state == STATE_ON
    assert hass.states.get(GROUP_0).attributes["level"] == 80


async def test_group_unavailable_without_members(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A group whose gear does not answer is unavailable."""
    fake_gateway.gear[16].present = False
    async_fire_time_changed(hass, dt_util.utcnow() + GEAR_POLL_INTERVAL)
    await _wait_for(hass, lambda: hass.states.get(GROUP_5).state == STATE_UNAVAILABLE)
    assert hass.states.get(GROUP_0).state == STATE_OFF


async def test_rescan_updates_groups(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Regrouped gear moves its group light, empty groups are removed."""
    fake_gateway.gear[16].groups = 1 << 7
    await hass.services.async_call(DOMAIN, "scan_bus", {}, blocking=True)
    await hass.async_block_till_done()
    assert get_device(device_registry, f"{SERIAL}_group5") is None
    assert hass.states.get(GROUP_5) is None
    assert hass.states.get("light.dali_group_7").attributes["entity_id"] == [LIGHT_A16]


async def test_remove_gear_removes_empty_group(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Removing the only member of a group removes the group light."""
    device = get_device(device_registry, f"{SERIAL}_cg16")
    assert await async_remove_config_entry_device(hass, setup_integration, device)
    await hass.async_block_till_done()
    assert get_device(device_registry, f"{SERIAL}_group5") is None
    group = get_device(device_registry, f"{SERIAL}_group0")
    assert not await async_remove_config_entry_device(hass, setup_integration, group)
