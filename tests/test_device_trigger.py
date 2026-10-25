"""Tests for push button device triggers."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.lunatone_wlan.const import DOMAIN, TRIGGER_TYPES
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from .conftest import SERIAL
from .fake_gateway import FakeGateway


def _device(device_registry: dr.DeviceRegistry, address: int) -> dr.DeviceEntry:
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{SERIAL}_cd{address}")}
    )
    assert device is not None
    return device


async def test_get_triggers(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Every button instance offers all trigger types."""
    device = _device(device_registry, 2)
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    expected = {
        (trigger_type, f"button_{number}")
        for number in range(4)
        for trigger_type in TRIGGER_TYPES
    }
    assert {(t["type"], t["subtype"]) for t in triggers} == expected


async def test_no_triggers_for_gateway(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The gateway device has no button triggers."""
    gateway = device_registry.async_get_device(identifiers={(DOMAIN, SERIAL)})
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, gateway.id
    )
    assert [t for t in triggers if t["domain"] == DOMAIN] == []


async def test_trigger_fires_automation(
    hass: HomeAssistant,
    fake_gateway: FakeGateway,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    service_calls: list[ServiceCall],
) -> None:
    """Short and double press of the same button trigger separately."""
    device = _device(device_registry, 1)
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device.id,
                        "type": trigger_type,
                        "subtype": "button_1",
                    },
                    "action": {
                        "service": "test.automation",
                        "data": {"trigger": trigger_type},
                    },
                }
                for trigger_type in ("short_press", "double_press")
            ]
        },
    )
    hub = setup_integration.runtime_data
    for frame in (
        [0x02, 0x84, 0x01],
        [0x02, 0x84, 0x00],
        [0x02, 0x84, 0x02],
        [0x02, 0x84, 0x05],
        [0x02, 0x80, 0x02],
    ):
        await fake_gateway.push_monitor(frame)
    await hub.gateway.send_frame([0xFF, 0xFE, 0x00])
    await hass.async_block_till_done()

    assert [call.data["trigger"] for call in service_calls] == [
        "short_press",
        "double_press",
    ]
