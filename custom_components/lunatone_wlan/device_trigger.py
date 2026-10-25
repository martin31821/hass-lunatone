"""Device triggers for DALI-2 push buttons."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import ATTR_INSTANCE, DOMAIN, EVENT_BUTTON, TRIGGER_TYPES
from .hub import LunatoneHub

CONF_SUBTYPE = "subtype"
SUBTYPE_PREFIX = "button_"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Required(CONF_SUBTYPE): vol.Match(rf"^{SUBTYPE_PREFIX}\d+$"),
    }
)


def _hub_and_address(
    hass: HomeAssistant, device_id: str
) -> tuple[LunatoneHub, int] | None:
    if (device := dr.async_get(hass).async_get(device_id)) is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            continue
        hub: LunatoneHub = entry.runtime_data
        if (address := hub.address_for_device(device)) is not None:
            return hub, address
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the button triggers of a control device."""
    if (found := _hub_and_address(hass, device_id)) is None:
        return []
    hub, address = found
    if (device := hub.devices.get(address)) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
            CONF_SUBTYPE: f"{SUBTYPE_PREFIX}{number}",
        }
        for number, instance in sorted(device.instances.items())
        if instance.is_button
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for button events of the device."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_BUTTON,
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                ATTR_INSTANCE: int(config[CONF_SUBTYPE].removeprefix(SUBTYPE_PREFIX)),
                CONF_TYPE: config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
