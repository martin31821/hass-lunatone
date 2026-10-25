"""Fixtures for the Lunatone DALI WLAN tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lunatone_wlan.const import DOMAIN
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .fake_gateway import FakeGateway, FakeInstance, cs_mini

SERIAL = "123456789012"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every test."""


@pytest.fixture(autouse=True)
def auto_enable_sockets(socket_enabled: None) -> None:
    """Allow sockets, the fake gateway listens on 127.0.0.1."""


@pytest.fixture
async def fake_gateway() -> AsyncGenerator[FakeGateway]:
    """Run a fake gateway with the MC2 modules from the real installation."""
    gateway = FakeGateway(
        devices={
            0: [FakeInstance(), FakeInstance(), FakeInstance(), FakeInstance()],
            1: [FakeInstance(), FakeInstance(), FakeInstance(), FakeInstance()],
            2: [
                FakeInstance(),
                FakeInstance(),
                FakeInstance(enabled=False),
                FakeInstance(enabled=False),
            ],
            3: [FakeInstance(), FakeInstance(enabled=False, event_scheme=0)],
            5: cs_mini(),
        }
    )
    await gateway.start()
    yield gateway
    await gateway.stop()


@pytest.fixture
def config_entry(fake_gateway: FakeGateway) -> MockConfigEntry:
    """Return a config entry pointing at the fake gateway."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Lunatone Dali WLAN",
        unique_id=SERIAL,
        data={CONF_HOST: fake_gateway.host},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration and wait for the initial bus scan."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    if (scan := config_entry.runtime_data.initial_scan) is not None:
        await scan
    await hass.async_block_till_done()
    return config_entry


def get_device(
    device_registry: dr.DeviceRegistry, identifier: str
) -> dr.DeviceEntry | None:
    """Return the device with an identifier of this integration."""
    devices = device_registry.async_get_devices(identifiers={(DOMAIN, identifier)})
    return devices[0] if devices else None
