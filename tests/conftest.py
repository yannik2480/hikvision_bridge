"""Shared fixtures: custom integration loading and simulated devices on localhost."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp.test_utils import TestServer
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_bridge.const import (
    CONF_ENTRY_KIND,
    CONF_SCHEME,
    CONF_SERIAL,
    CONF_VERIFY_TLS,
    DOMAIN,
    KIND_DEVICE,
)
from tests.fakedevice import FakeDevice


@pytest.fixture(autouse=True)
def load_custom_integrations(enable_custom_integrations: None) -> None:
    """Load integrations from custom_components in every test."""


@pytest.fixture
async def serve(socket_enabled: None) -> Callable[[FakeDevice], Awaitable[TestServer]]:
    servers: list[TestServer] = []

    async def start(device: FakeDevice) -> TestServer:
        server = TestServer(device.app())
        await server.start_server()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.close()


def device_entry(
    server: TestServer, device: FakeDevice, options: dict | None = None, **overrides
) -> MockConfigEntry:
    """Config entry as the config flow would store it for this simulated device."""
    data = {
        CONF_ENTRY_KIND: KIND_DEVICE,
        CONF_HOST: server.host,
        CONF_PORT: server.port,
        CONF_SCHEME: "http",
        CONF_VERIFY_TLS: True,
        CONF_USERNAME: device.username,
        CONF_PASSWORD: device.password,
        CONF_SERIAL: None,
        **overrides,
    }
    return MockConfigEntry(
        domain=DOMAIN, data=data, title="Test recorder", version=1, options=options or {}
    )
