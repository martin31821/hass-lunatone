"""Tests for the services and the scan button."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.lunatone_wlan.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .fake_gateway import FakeGateway, FakeInstance


@pytest.mark.parametrize(
    ("data", "frame"),
    [
        ({"scene": 1}, [255, 17]),
        ({"scene": 2, "group": 0}, [129, 18]),
        ({"scene": 0, "group": 6}, [141, 16]),
        ({"scene": 15, "address": 3}, [7, 31]),
    ],
)
async def test_go_to_scene(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    data: dict,
    frame: list[int],
) -> None:
    """Scenes are recalled on all lights, a group or one address."""
    await hass.services.async_call(DOMAIN, "go_to_scene", data, blocking=True)
    message = fake_gateway.received[-1]["data"]
    assert message["daliData"] == frame
    assert message["numberOfBits"] == 16
    assert message["mode"]["sendTwice"] is False


@pytest.mark.parametrize(
    "data",
    [
        {"scene": 16},
        {"scene": 1, "group": 16},
        {"scene": 1, "address": 64},
        {"scene": 1, "group": 1, "address": 1},
    ],
)
async def test_go_to_scene_invalid(
    hass: HomeAssistant, setup_integration: MockConfigEntry, data: dict
) -> None:
    """Out of range targets are rejected."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "go_to_scene", data, blocking=True)


async def test_send_frame(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Raw frames are sent and answers returned."""
    response = await hass.services.async_call(
        DOMAIN,
        "send_frame",
        {"data": "07 FE 35", "wait_for_answer": True},
        blocking=True,
        return_response=True,
    )
    assert response == {"answer": 2}
    response = await hass.services.async_call(
        DOMAIN,
        "send_frame",
        {"data": [7, 0, 0x67], "send_twice": True},
        blocking=True,
        return_response=True,
    )
    assert response == {"answer": None}
    assert fake_gateway.received[-1]["data"]["mode"]["sendTwice"] is True


@pytest.mark.parametrize("data", ["ZZ", "01", [1, 2, 3, 4], [1, 300]])
async def test_send_frame_invalid(
    hass: HomeAssistant, setup_integration: MockConfigEntry, data
) -> None:
    """Malformed frames are rejected."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "send_frame", {"data": data}, blocking=True
        )


async def test_send_failure(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """Gateway errors surface as Home Assistant errors."""
    fake_gateway.ack_result = 1
    with pytest.raises(HomeAssistantError, match="result 1"):
        await hass.services.async_call(
            DOMAIN, "go_to_scene", {"scene": 1}, blocking=True
        )


async def test_no_gateway(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Services need a loaded gateway."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "go_to_scene",
            {"scene": 1, "config_entry_id": "nope"},
            blocking=True,
        )
    await hass.config_entries.async_unload(setup_integration.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "go_to_scene", {"scene": 1}, blocking=True
        )


async def test_multiple_gateways(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """With two gateways the target has to be chosen."""
    second = MockConfigEntry(
        domain=DOMAIN, unique_id="second", data={CONF_HOST: fake_gateway.host}
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "go_to_scene", {"scene": 1}, blocking=True
        )
    await hass.services.async_call(
        DOMAIN,
        "go_to_scene",
        {"scene": 1, "config_entry_id": second.entry_id},
        blocking=True,
    )
    await hass.config_entries.async_unload(second.entry_id)


async def test_scan_bus_service(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """A scan returns the devices and adds new ones."""
    fake_gateway.devices[9] = [FakeInstance(instance_type=3)]
    response = await hass.services.async_call(
        DOMAIN, "scan_bus", {}, blocking=True, return_response=True
    )
    assert response["devices"]["A3"] == {
        "0": {"instance_type": 1, "enabled": True, "event_scheme": 2, "resolution": 1},
        "1": {"instance_type": 1, "enabled": False, "event_scheme": 0, "resolution": 1},
    }
    assert response["devices"]["A9"] == {
        "0": {"instance_type": 3, "enabled": True, "event_scheme": 2, "resolution": 1}
    }
    await hass.async_block_till_done()
    # Only push buttons get event entities
    assert hass.states.get("event.dali_input_device_a9_button_0") is None


async def test_scan_warns_about_unaddressed_events(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enabled inputs without device address in their events are reported."""
    fake_gateway.devices[3][1] = FakeInstance(event_scheme=0)
    await hass.services.async_call(DOMAIN, "scan_bus", {}, blocking=True)
    assert "device/instance: A3 instance 1" in caplog.text


async def test_scan_button(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """The scan button queries every short address."""
    fake_gateway.received.clear()
    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: "button.lunatone_dali_wlan_scan_bus"},
        blocking=True,
    )
    queried = {
        frame[0] >> 1 for frame in fake_gateway.frames if frame[1:] == [0xFE, 0x35]
    }
    assert queried == set(range(64))
