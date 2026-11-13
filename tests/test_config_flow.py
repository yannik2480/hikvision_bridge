"""Config, reauth and reconfigure flows of device entries and the cloud flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.hikvision_bridge import panel
from custom_components.hikvision_bridge.cloud.account import CaptchaRequired, CloudAuthFailed
from custom_components.hikvision_bridge.config_flow import Address, field_errors, split_address
from custom_components.hikvision_bridge.const import (
    CONF_ACCOUNT,
    CONF_CONNECTION,
    CONF_ENTRY_KIND,
    CONF_SCHEME,
    CONF_SERIAL,
    DOMAIN,
    KIND_CLOUD,
    KIND_DEVICE,
    OPT_PANEL_ADMIN_ONLY,
    OPT_PANEL_HEADER,
    OPT_PANEL_TITLE,
    OPT_PLAYBACK,
    OPT_RTSP_PORT,
    OPT_SIDEBAR_PANEL,
    PLAYBACK_SEEKABLE,
)
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice


async def _device_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )


def _form_input(server, device: FakeDevice, connection: str = "http", **extra) -> dict:
    return {
        CONF_HOST: server.host,
        CONF_PORT: server.port,
        CONF_CONNECTION: connection,
        "verify_tls": True,
        CONF_USERNAME: device.username,
        CONF_PASSWORD: device.password,
        **extra,
    }


async def test_device_flow_creates_entry_keyed_by_serial(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    form = await _device_form(hass)
    with patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], _form_input(server, device)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front Yard"
    assert result["result"].unique_id == "testserial201"
    assert result["data"][CONF_ENTRY_KIND] == KIND_DEVICE
    assert result["data"][CONF_SCHEME] == "http"
    assert result["data"][CONF_SERIAL] == "TESTSERIAL201"
    # New entries play recordings seekable; entries without the option keep MP4.
    assert result["result"].options == {OPT_PLAYBACK: PLAYBACK_SEEKABLE}
    assert {method for method, _ in device.requests} == {"GET"}


async def test_auto_connection_falls_back_to_http(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    form = await _device_form(hass)
    with patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], _form_input(server, device, connection="auto")
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SCHEME] == "http"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.0.2.10", Address("192.0.2.10")),
        (" 192.0.2.10 ", Address("192.0.2.10")),
        ("cam.local:8080", Address("cam.local", 8080)),
        (
            "rtsp://user:secret@192.0.2.10:8554/Streaming/Channels/102",
            Address("192.0.2.10", rtsp_port=8554),
        ),
        ("rtsp://192.0.2.10/Streaming/Channels/101", Address("192.0.2.10")),
        ("https://192.0.2.10:8443/doc/index.html", Address("192.0.2.10", 8443, "https")),
        ("http://user@cam.local/", Address("cam.local", scheme="http")),
        ("fe80::1", Address("fe80::1")),
        ("[fe80::1]", Address("fe80::1")),
        ("rtsp://[fe80::1]:554/", Address("fe80::1", rtsp_port=554)),
    ],
)
def test_pasted_urls_are_reduced_to_the_host(text: str, expected: Address) -> None:
    assert split_address(text) == expected


async def test_pasted_rtsp_url_sets_up_the_device(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    form = await _device_form(hass)
    url = f"rtsp://{device.username}:{device.password}@{server.host}:8554/Streaming/Channels/102"
    with patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], _form_input(server, device, connection="auto", host=url)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == server.host
    assert result["data"][CONF_SCHEME] == "http"
    assert result["result"].options[OPT_RTSP_PORT] == 8554


async def test_wrong_password_reports_once_without_retry(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    form = await _device_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], _form_input(server, device, connection="auto", password="nope")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert device.failed_logins == 1


async def test_empty_and_out_of_range_fields_are_marked_beside_the_field(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    form = await _device_form(hass)
    for key in (CONF_HOST, CONF_USERNAME, CONF_PASSWORD):
        assert not isinstance(_schema_key(form, key), vol.Required), key
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        {CONF_HOST: "  ", CONF_PORT: 70000, CONF_CONNECTION: "http", "verify_tls": True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_HOST: "required",
        CONF_PORT: "invalid_port",
        CONF_USERNAME: "required",
        CONF_PASSWORD: "required",
    }
    assert device.requests == []
    with patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _form_input(server, device)
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


def test_field_errors_leave_filled_fields_alone() -> None:
    assert field_errors({"a": "x", "p": 554}, ("a",), {"p": ((1, 65535), "invalid_port")}) == {}
    assert field_errors({"p": 0}, ("a",), {"p": ((1, 65535), "invalid_port")}) == {
        "a": "required",
        "p": "invalid_port",
    }


def _schema_key(form, name: str):
    return next(key for key in form["data_schema"].schema if key == name)


async def test_same_device_twice_aborts(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial201")
    form = await _device_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], _form_input(server, device)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    entry = device_entry(server, device, password="old")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial201")

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: device.username, CONF_PASSWORD: device.password}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == device.password


async def test_reconfigure_refuses_other_device(hass: HomeAssistant, serve) -> None:
    recorder = FakeDevice("nvr")
    camera = FakeDevice("camera_g3")
    recorder_server = await serve(recorder)
    camera_server = await serve(camera)
    entry = device_entry(recorder_server, recorder)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial101")

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _form_input(camera_server, camera)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "other_device"
    assert entry.data[CONF_HOST] == recorder_server.host
    assert entry.data[CONF_PORT] == recorder_server.port


async def _cloud_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )


async def test_cloud_flow_creates_entry(hass: HomeAssistant) -> None:
    form = await _cloud_form(hass)
    with (
        patch(
            "custom_components.hikvision_bridge.cloud.account.CloudAccount.login",
            return_value=None,
        ),
        patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ACCOUNT: "someone@example.invalid", CONF_PASSWORD: "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTRY_KIND] == KIND_CLOUD
    assert result["result"].unique_id.startswith("cloud_")
    assert "someone" not in result["result"].unique_id
    assert result["title"] == "Hik-Connect (s***@example.invalid)"


async def test_cloud_flow_bad_login_and_captcha(hass: HomeAssistant) -> None:
    form = await _cloud_form(hass)
    login = "custom_components.hikvision_bridge.cloud.account.CloudAccount.login"
    with patch(login, side_effect=CloudAuthFailed()):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ACCOUNT: "a@example.invalid", CONF_PASSWORD: "pw"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(login, side_effect=CaptchaRequired("https://example.invalid/c.png")):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_ACCOUNT: "a@example.invalid", CONF_PASSWORD: "pw"}
        )
    assert result["step_id"] == "cloud_captcha"

    with (
        patch(login, return_value=None) as mocked,
        patch("custom_components.hikvision_bridge.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"captcha": "1234"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mocked.call_args.kwargs == {"captcha": "1234"}


async def test_panel_options_apply_to_the_sidebar(hass: HomeAssistant, hass_storage) -> None:
    from homeassistant.components.frontend import DATA_PANELS  # noqa: PLC0415

    state = panel._state(hass)
    state.url = "/hikvision_bridge_static/hikvision-bridge.js"
    await panel._async_apply(hass)
    assert not hass.data[DATA_PANELS][panel.PANEL_PATH].require_admin

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ENTRY_KIND: KIND_CLOUD}, title="Cloud")
    entry.add_to_hass(hass)

    async def configure(**values: object) -> None:
        form = await hass.config_entries.options.async_init(entry.entry_id)
        assert set(form["data_schema"].schema) == {
            OPT_SIDEBAR_PANEL,
            OPT_PANEL_ADMIN_ONLY,
            OPT_PANEL_TITLE,
            OPT_PANEL_HEADER,
        }
        result = await hass.config_entries.options.async_configure(form["flow_id"], values)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {}

    await configure(sidebar_panel=True, panel_admin_only=True)
    assert hass.data[DATA_PANELS][panel.PANEL_PATH].require_admin
    assert hass.data[DATA_PANELS][panel.PANEL_PATH].sidebar_title == "Hikvision"

    await configure(
        sidebar_panel=True, panel_admin_only=True, panel_title="  My   house ", panel_header=False
    )
    registered = hass.data[DATA_PANELS][panel.PANEL_PATH]
    assert registered.sidebar_title == "My house"
    assert registered.config["title"] == "My house"
    assert registered.config["header"] is False
    # An emptied title field is left out by the browser: back to the default.
    await configure(sidebar_panel=True, panel_admin_only=True, panel_header=True)
    assert hass.data[DATA_PANELS][panel.PANEL_PATH].sidebar_title == "Hikvision"
    await configure(sidebar_panel=False, panel_admin_only=True)
    assert panel.PANEL_PATH not in hass.data[DATA_PANELS]
    assert hass_storage[panel.STORE_KEY]["data"] == {
        OPT_SIDEBAR_PANEL: False,
        OPT_PANEL_ADMIN_ONLY: True,
        OPT_PANEL_TITLE: "Hikvision",
        OPT_PANEL_HEADER: True,
    }

    # After a restart the stored choice holds.
    del hass.data[panel.DATA_PANEL]
    await panel.async_register_frontend(hass)
    assert panel.panel_settings(hass)[OPT_SIDEBAR_PANEL] is False
