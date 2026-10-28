"""The Lunatone DALI WLAN integration."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .gateway import GatewayConnectionError, LunatoneGateway
from .hub import LunatoneConfigEntry, LunatoneHub
from .services import async_setup_services

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LunatoneConfigEntry) -> bool:
    """Connect to the gateway and start listening to the bus."""
    host = entry.data[CONF_HOST]
    gateway = LunatoneGateway(async_get_clientsession(hass), host)
    try:
        await gateway.connect()
    except GatewayConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hub = LunatoneHub(hass, entry, gateway)
    await hub.async_setup()
    entry.runtime_data = hub
    entry.async_create_background_task(hass, gateway.run(), f"{DOMAIN} {host}")

    async def close_on_stop(event: Event) -> None:
        await gateway.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_on_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Scan on every start, devices may have been re-addressed
    hub.initial_scan = entry.async_create_background_task(
        hass, hub.async_initial_scan(), f"{DOMAIN} {host} bus scan"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LunatoneConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: LunatoneConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing control devices and gear, they come back with the next scan."""
    hub = entry.runtime_data
    if (address := hub.address_for_device(device)) is not None:
        hub.async_remove_device(address)
        return True
    if (address := hub.gear_address_for_device(device)) is not None:
        hub.async_remove_gear(address)
        return True
    return False
