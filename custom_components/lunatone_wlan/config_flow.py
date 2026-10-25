"""Config flow for the Lunatone DALI WLAN integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .gateway import GatewayConnectionError, LunatoneGateway

DATA_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


class LunatoneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a gateway by host name or IP address."""

    VERSION = 1

    async def _async_probe(self, host: str) -> dict[str, Any]:
        gateway = LunatoneGateway(async_get_clientsession(self.hass), host)
        await gateway.connect()
        await gateway.close()
        return gateway.info

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                info = await self._async_probe(host)
            except GatewayConnectionError:
                errors["base"] = "cannot_connect"
            else:
                serial = info.get("device", {}).get("serial")
                await self.async_set_unique_id(str(serial) if serial else host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=info.get("name") or "Lunatone DALI WLAN",
                    data={CONF_HOST: host},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(DATA_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the gateway address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                info = await self._async_probe(host)
            except GatewayConnectionError:
                errors["base"] = "cannot_connect"
            else:
                serial = info.get("device", {}).get("serial")
                await self.async_set_unique_id(str(serial) if serial else host)
                self._abort_if_unique_id_mismatch(reason="wrong_gateway")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA, user_input or entry.data
            ),
            errors=errors,
        )
