"""Services for the Lunatone DALI WLAN integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ADDRESS,
    ATTR_ANSWER,
    ATTR_DATA,
    ATTR_GROUP,
    ATTR_SCENE,
    ATTR_SEND_TWICE,
    ATTR_WAIT_FOR_ANSWER,
    CONF_CONFIG_ENTRY_ID,
    DOMAIN,
)
from .dali import (
    BROADCAST,
    MAX_GROUP,
    MAX_SCENE,
    MAX_SHORT_ADDRESS,
    go_to_scene,
    group_address,
    short_address,
)
from .gateway import GatewayError
from .hub import LunatoneHub

SERVICE_GO_TO_SCENE = "go_to_scene"
SERVICE_SEND_FRAME = "send_frame"
SERVICE_SCAN_BUS = "scan_bus"


def _frame(value: Any) -> list[int]:
    """Accept a list of bytes or a hex string such as "8D10"."""
    if isinstance(value, str):
        text = value.replace(" ", "").removeprefix("0x")
        try:
            data = list(bytes.fromhex(text))
        except ValueError as err:
            raise vol.Invalid(f"Invalid hex frame {value!r}") from err
    else:
        data = [vol.All(vol.Coerce(int), vol.Range(0, 0xFF))(byte) for byte in value]
    if len(data) not in (2, 3):
        raise vol.Invalid("A frame has 2 bytes (16 bit) or 3 bytes (24 bit)")
    return data


ENTRY_SCHEMA = {vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string}

GO_TO_SCENE_SCHEMA = vol.Schema(
    {
        **ENTRY_SCHEMA,
        vol.Required(ATTR_SCENE): vol.All(vol.Coerce(int), vol.Range(0, MAX_SCENE)),
        vol.Exclusive(ATTR_GROUP, "target"): vol.All(
            vol.Coerce(int), vol.Range(0, MAX_GROUP)
        ),
        vol.Exclusive(ATTR_ADDRESS, "target"): vol.All(
            vol.Coerce(int), vol.Range(0, MAX_SHORT_ADDRESS)
        ),
    }
)

SEND_FRAME_SCHEMA = vol.Schema(
    {
        **ENTRY_SCHEMA,
        vol.Required(ATTR_DATA): _frame,
        vol.Optional(ATTR_SEND_TWICE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_ANSWER, default=False): cv.boolean,
    }
)

SCAN_BUS_SCHEMA = vol.Schema(ENTRY_SCHEMA)


def _get_hub(hass: HomeAssistant, call: ServiceCall) -> LunatoneHub:
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry_id": entry_id},
            )
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_gateway"
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="multiple_gateways"
        )
    return entries[0].runtime_data


async def _send(hub: LunatoneHub, frame: list[int], **kwargs: Any) -> int | None:
    try:
        return await hub.gateway.send_frame(frame, **kwargs)
    except GatewayError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_failed",
            translation_placeholders={"error": str(err)},
        ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register services."""

    async def go_to_scene_service(call: ServiceCall) -> None:
        hub = _get_hub(hass, call)
        if (group := call.data.get(ATTR_GROUP)) is not None:
            address_byte = group_address(group)
        elif (address := call.data.get(ATTR_ADDRESS)) is not None:
            address_byte = short_address(address)
        else:
            address_byte = BROADCAST
        await _send(hub, go_to_scene(address_byte, call.data[ATTR_SCENE]))

    async def send_frame_service(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        answer = await _send(
            hub,
            call.data[ATTR_DATA],
            send_twice=call.data[ATTR_SEND_TWICE],
            wait_for_answer=call.data[ATTR_WAIT_FOR_ANSWER],
        )
        return {ATTR_ANSWER: answer}

    async def scan_bus_service(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        try:
            return {"devices": await hub.async_scan()}
        except GatewayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="send_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    hass.services.async_register(
        DOMAIN, SERVICE_GO_TO_SCENE, go_to_scene_service, schema=GO_TO_SCENE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_FRAME,
        send_frame_service,
        schema=SEND_FRAME_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_BUS,
        scan_bus_service,
        schema=SCAN_BUS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
