"""Regression tests for the security audit findings fixed in services, flows and the scrubber."""

# ruff: noqa: F811 - the recorder fixture is imported and used as a parameter

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_bridge import async_remove_entry, config_flow
from custom_components.hikvision_bridge.const import DOMAIN, SUBENTRY_CHANNEL_LINK
from custom_components.hikvision_bridge.wire import faults
from custom_components.hikvision_bridge.wire.link import Reply
from custom_components.hikvision_bridge.wire.scrub import LOG_SCRUBBER, MASK, scrub_text
from tests.test_actions_and_links import _main_device, recorder  # noqa: F401


async def test_isapi_request_is_admin_only(
    hass: HomeAssistant,
    recorder,
    hass_read_only_user,
) -> None:
    device, entry = recorder
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {
                "device_id": _main_device(hass, entry),
                "method": "PUT",
                "path": "/ISAPI/System/reboot",
                "confirm_write": True,
            },
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    assert device.writes == []


@pytest.mark.parametrize(
    "service", ["add_center_links", "set_notification_host", "cloud_delete_area"]
)
def test_configuring_services_are_admin_only(service: str) -> None:
    source = (Path(config_flow.__file__).parent / "services.py").read_text("utf-8")
    assert (
        f'async_register_admin_service(\n        hass,\n        DOMAIN,\n        "{service}"'
        in (source)
    )


@pytest.mark.parametrize(
    "path", ["/ISAPI/../SDK/x", "/ISAPI/System/%2e%2e/%2E%2E/x", "/ISAPI/./System"]
)
async def test_isapi_request_refuses_dot_segments(
    hass: HomeAssistant,
    recorder,
    path: str,
) -> None:
    _, entry = recorder
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {"device_id": _main_device(hass, entry), "path": path},
            blocking=True,
            return_response=True,
        )


async def test_isapi_request_masks_echoed_credentials(
    hass: HomeAssistant,
    recorder,
) -> None:
    device, entry = recorder
    echoed = f"<User><name>{device.username}</name><pw>{device.password}</pw></User>"
    reply = Reply(200, "application/xml", echoed.encode(), 1.0)
    with patch.object(entry.runtime_data.link, "call", AsyncMock(return_value=reply)):
        response = await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {"device_id": _main_device(hass, entry), "path": "/ISAPI/Security/users"},
            blocking=True,
            return_response=True,
        )
    assert device.password not in response["body"]
    assert MASK in response["body"]


def test_compound_secret_names_are_masked() -> None:
    text = (
        "<ftpPassword>ftp-secret</ftpPassword><wpaKey>wifi-secret</wpaKey>"
        '<smtpPwd>mail-secret</smtpPwd>{"loginPassword": "json-secret"}'
        "<channelName>Garden</channelName>"
    )
    cleaned = scrub_text(text)
    for secret in ("ftp-secret", "wifi-secret", "mail-secret", "json-secret"):
        assert secret not in cleaned
    assert "Garden" in cleaned


async def test_removing_one_entry_keeps_a_shared_password_masked(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    shared = "shared-pass-123"
    first = MockConfigEntry(domain=DOMAIN, data={CONF_USERNAME: "admin", CONF_PASSWORD: shared})
    second = MockConfigEntry(domain=DOMAIN, data={CONF_USERNAME: "admin", CONF_PASSWORD: shared})
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    LOG_SCRUBBER.remember("admin", shared)
    await async_remove_entry(hass, first)
    logger = logging.getLogger("custom_components.hikvision_bridge.test")
    logger.addFilter(LOG_SCRUBBER)
    with caplog.at_level(logging.WARNING):
        logger.warning("login with %s", shared)
    assert shared not in caplog.text


async def test_auto_mode_does_not_fall_back_to_http_after_untrusted_certificate(
    hass: HomeAssistant,
) -> None:
    probe = AsyncMock(side_effect=faults.TlsUntrusted("self-signed"))
    data = {
        CONF_HOST: "192.0.2.10",
        "connection": "auto",
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw-123",
    }
    with (
        patch.object(config_flow, "read_identity_only", probe),
        pytest.raises(faults.TlsUntrusted),
    ):
        await config_flow.probe_device(hass, data)
    assert probe.await_count == 1
    (call,) = probe.await_args_list
    assert call.args[0].endpoint.scheme == "https"


async def _channel_link(hass: HomeAssistant, entry, host: str, port: int, *, reuse: bool) -> dict:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CHANNEL_LINK), context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "channel_id": "1",
            CONF_HOST: host,
            CONF_PORT: port,
            "connection": "http",
            "verify_tls": True,
            "reuse_recorder_credentials": reuse,
            CONF_USERNAME: "camera-user",
            CONF_PASSWORD: "camera-pass",
            "event_source": "recorder",
        },
    )


async def test_recorder_credentials_go_only_to_vouched_addresses(
    hass: HomeAssistant,
    recorder,
) -> None:
    _, entry = recorder
    probe = AsyncMock()
    with patch.object(config_flow, "probe_device", probe):
        result = await _channel_link(hass, entry, "198.51.100.7", 80, reuse=True)
    assert result["errors"] == {"base": "reuse_unknown_address"}
    probe.assert_not_awaited()


async def test_camera_without_serial_cannot_take_a_channel(
    hass: HomeAssistant,
    recorder,
) -> None:
    _, entry = recorder
    resolved = {CONF_HOST: "198.51.100.7", CONF_PORT: 80, "scheme": "http", "verify_tls": True}
    probe = AsyncMock(return_value=(resolved, SimpleNamespace(serial="")))
    with patch.object(config_flow, "probe_device", probe):
        result = await _channel_link(hass, entry, "198.51.100.7", 80, reuse=False)
    assert result["errors"] == {"base": "wrong_camera"}
