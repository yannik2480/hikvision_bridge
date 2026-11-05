"""Actions, the linkage repair, duplicate detection and direct camera connections."""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import urlsplit

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.entity_platform import async_get_platforms
import pytest

from custom_components.hikvision_bridge import repairs
from custom_components.hikvision_bridge.const import DOMAIN, SUBENTRY_CHANNEL_LINK
from custom_components.hikvision_bridge.runtime.bridge import direct_rtsp_port
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice


@pytest.fixture
async def recorder(hass: HomeAssistant, serve):
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial101")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield device, entry
    await hass.async_block_till_done()
    if entry.state is config_entries.ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def _main_device(hass: HomeAssistant, entry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "testserial101"), entry.entry_id
    )
    return device.id


async def test_isapi_request_reads_and_masks(hass: HomeAssistant, recorder) -> None:
    _, entry = recorder
    response = await hass.services.async_call(
        DOMAIN,
        "isapi_request",
        {"device_id": _main_device(hass, entry), "path": "/ISAPI/System/deviceInfo"},
        blocking=True,
        return_response=True,
    )
    assert response["ok"]
    assert "TESTSERIAL101" not in response["body"]
    assert "02:00:00:00:01:01" not in response["body"]


async def test_isapi_request_refuses_unconfirmed_writes(hass: HomeAssistant, recorder) -> None:
    device, entry = recorder
    target = _main_device(hass, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {"device_id": target, "method": "PUT", "path": "/ISAPI/System/reboot"},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {"device_id": target, "path": "/SDK/whatever"},
            blocking=True,
            return_response=True,
        )
    assert device.writes == []


async def test_add_center_links_previews_then_writes(hass: HomeAssistant, recorder) -> None:
    device, entry = recorder
    target = _main_device(hass, entry)
    preview = await hass.services.async_call(
        DOMAIN, "add_center_links", {"device_id": target}, blocking=True, return_response=True
    )
    assert preview["dry_run"] is True
    assert preview["triggers"]
    assert device.writes == []

    done = await hass.services.async_call(
        DOMAIN,
        "add_center_links",
        {"device_id": target, "dry_run": False, "kinds": ["intrusion"]},
        blocking=True,
        return_response=True,
    )
    assert done["triggers"]
    assert all("center" in body for _, _, body in device.writes)
    assert {path for _, path, _ in device.writes} <= {
        f"/ISAPI/Event/triggers/{t}" for t in done["triggers"]
    }


async def test_notification_host_needs_webhook(hass: HomeAssistant, recorder) -> None:
    device, entry = recorder
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_notification_host",
            {"device_id": _main_device(hass, entry), "confirm_write": True},
            blocking=True,
            return_response=True,
        )
    assert device.writes == []


async def test_center_link_repair_writes_only_after_confirmation(
    hass: HomeAssistant, recorder
) -> None:
    device, entry = recorder
    await entry.runtime_data.async_run_audit()
    issues = [
        (issue_id, issue)
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "center_link_missing"
    ]
    assert issues
    issue_id, issue = issues[0]
    assert issue.is_fixable
    flow = await repairs.async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    menu = await flow.async_step_init()
    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == ["confirm", "ignore"]
    assert device.writes == []

    # Ignore flow marks the issue dismissed in Home Assistant
    ignore_flow = await repairs.async_create_fix_flow(hass, issue_id, issue.data)
    ignore_flow.hass = hass
    ignored = await ignore_flow.async_step_ignore()
    assert ignored["type"] is FlowResultType.ABORT
    assert ignored["reason"] == "issue_ignored"
    issue_entry = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue_entry.dismissed_version is not None

    # Confirm flow writes the linkage change and reloads the entry
    done = await flow.async_step_confirm()
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert len(device.writes) == 1


async def test_camera_already_on_recorder_offers_link(hass: HomeAssistant, recorder, serve) -> None:
    _, entry = recorder
    camera = FakeDevice("camera_g3", password="camera-own-pass")
    server = await serve(camera)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            "connection": "http",
            "verify_tls": True,
            CONF_USERNAME: camera.username,
            CONF_PASSWORD: camera.password,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "duplicate"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "link_to_channel"}
    )
    assert result["reason"] == "linked_to_channel"
    (link,) = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL_LINK]
    assert link.unique_id == "1"
    assert link.data[CONF_PASSWORD] == "camera-own-pass"


async def test_direct_link_refuses_wrong_camera(hass: HomeAssistant, recorder, serve) -> None:
    _, entry = recorder
    other = FakeDevice("camera_noio")
    server = await serve(other)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CHANNEL_LINK), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "channel_id": "1",
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            "connection": "http",
            "verify_tls": True,
            "reuse_recorder_credentials": False,
            CONF_USERNAME: other.username,
            CONF_PASSWORD: other.password,
            "event_source": "recorder",
        },
    )
    assert result["errors"] == {"base": "wrong_camera"}


async def _stream_source_of_direct_link(
    hass: HomeAssistant, entry, serve, extra: dict
) -> tuple[str, int]:
    camera = FakeDevice("camera_g3", password="camera-own-pass")
    server = await serve(camera)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CHANNEL_LINK), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "channel_id": "1",
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            "connection": "http",
            "verify_tls": True,
            "reuse_recorder_credentials": False,
            CONF_USERNAME: camera.username,
            CONF_PASSWORD: camera.password,
            "event_source": "recorder",
            **extra,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # Home Assistant 2026.9 keeps the emptied camera platforms of earlier setups.
    cameras = [
        e
        for p in async_get_platforms(hass, DOMAIN)
        if p.domain == "camera"
        for e in p.entities.values()
    ]
    main = next(e for e in cameras if e.channel_id == "1" and e._stream.rank == 1)
    return await main.stream_source(), server.port


async def test_direct_link_streams_through_recorder_port_forwarding(
    hass: HomeAssistant, recorder, serve
) -> None:
    """A camera at the recorder's address is a forwarded port, which also carries RTSP."""
    _, entry = recorder
    source, port = await _stream_source_of_direct_link(hass, entry, serve, {})
    assert source.endswith(f":{port}/Streaming/Channels/101")


async def test_direct_link_rtsp_port_can_be_set(hass: HomeAssistant, recorder, serve) -> None:
    _, entry = recorder
    source, _ = await _stream_source_of_direct_link(hass, entry, serve, {"rtsp_port": 65002})
    assert source.endswith(":65002/Streaming/Channels/101")
    (link,) = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL_LINK]
    assert link.data["rtsp_port"] == 65002


def test_direct_link_elsewhere_uses_default_rtsp_port() -> None:
    data = {CONF_HOST: "198.51.100.7", CONF_PORT: 80}
    assert direct_rtsp_port(data, "203.0.113.5") == 554
    assert direct_rtsp_port({**data, CONF_HOST: "203.0.113.5 "}, "203.0.113.5") == 80


async def test_direct_link_can_leave_live_stream_on_recorder(
    hass: HomeAssistant, recorder, serve
) -> None:
    """Live stream from the recorder; the direct link still serves everything else."""
    nvr, entry = recorder
    source, port = await _stream_source_of_direct_link(
        hass, entry, serve, {"stream_source": "recorder"}
    )
    url = urlsplit(source)
    assert (url.username, url.password) == (nvr.username, nvr.password)
    assert f":{port}/" not in source
    assert source.endswith("/Streaming/Channels/101")
    bridge = entry.runtime_data
    assert bridge.routes["1"].stream_from_recorder
    assert bridge.route("1")[0] is bridge.routes["1"].link
    (link,) = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL_LINK]
    assert link.data["stream_source"] == "recorder"


async def test_channel_link_changes_reload_without_update_listener(
    hass: HomeAssistant, recorder, serve
) -> None:
    _, entry = recorder
    assert not entry.update_listeners
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(entry, title="renamed")
        await hass.async_block_till_done()
        reload.assert_not_called()
        await _stream_source_of_direct_link(hass, entry, serve, {})
    reload.assert_called_with(entry.entry_id)
    (link,) = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL_LINK]
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_remove_subentry(entry, link.subentry_id)
        await hass.async_block_till_done()
    reload.assert_called_with(entry.entry_id)
