"""Tests for the config flow."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lunatone_wlan.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import SERIAL
from .fake_gateway import FakeGateway


async def test_user_flow(hass: HomeAssistant, fake_gateway: FakeGateway) -> None:
    """The gateway is added by host and identified by its serial."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: f" {fake_gateway.host} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Lunatone Dali WLAN"
    assert result["data"] == {CONF_HOST: fake_gateway.host}
    assert result["result"].unique_id == SERIAL
    await hass.async_block_till_done()


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    """Unreachable hosts show an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1:1"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, fake_gateway: FakeGateway
) -> None:
    """The same gateway cannot be added twice, its host is updated."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SERIAL, data={CONF_HOST: "old"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_gateway.host}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == fake_gateway.host


async def test_reconfigure(
    hass: HomeAssistant, fake_gateway: FakeGateway, setup_integration: MockConfigEntry
) -> None:
    """The host of an existing gateway can be changed."""
    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1:1"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_gateway.host}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def test_reconfigure_wrong_gateway(
    hass: HomeAssistant, fake_gateway: FakeGateway
) -> None:
    """A different gateway is not accepted when reconfiguring."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="other", data={CONF_HOST: "x"})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_gateway.host}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_gateway"
