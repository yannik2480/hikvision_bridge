"""Setup, unload, device tree, diagnostics and failure handling of device entries."""

from __future__ import annotations

import json

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from custom_components.hikvision_bridge.const import DOMAIN, OPT_TRY_ENCRYPTED_STREAM
from custom_components.hikvision_bridge.diagnostics import async_get_config_entry_diagnostics
from custom_components.hikvision_bridge.model.discover import CLOUD_PATH
from tests.conftest import device_entry
from tests.fakedevice import FIXTURES, FakeDevice


async def _set_up_entry(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_recorder_setup_builds_device_tree_and_unloads(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_get(hass)
    main = devices.async_get_device_by_identifier((DOMAIN, "testserial101"), entry.entry_id)
    assert main is not None
    assert main.serial_number == "TESTSERIAL101"
    channels = [
        d for d in dr.async_entries_for_config_entry(devices, entry.entry_id) if d.via_device_id
    ]
    assert channels, "recorder channels must be separate devices below the recorder"
    assert all(d.via_device_id == main.id for d in channels)

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    domains = {e.domain for e in entities}
    assert {Platform.BINARY_SENSOR, Platform.SWITCH, Platform.CAMERA} <= domains
    # unique ids are derived from the serial, never from the address
    assert all(e.unique_id.startswith("testserial101_") for e in entities)
    assert all(server.host not in e.unique_id for e in entities)
    sub_streams = [e for e in entities if e.unique_id.endswith("_stream_2")]
    assert sub_streams, "sub streams must be offered"
    assert all(e.disabled_by is None for e in sub_streams)

    # setup only reads
    assert {method for method, _ in device.requests} == {"GET"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_stream_encryption_is_followed_without_reload(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    sensor = next(e.entity_id for e in entities if e.unique_id.endswith("_stream_encryption"))
    camera = next(e.entity_id for e in entities if e.unique_id.endswith("_stream_1"))
    issue_id = f"{entry.entry_id}_audit_stream_encrypted_device"

    assert hass.states.get(sensor).state == STATE_ON
    assert hass.states.get(camera).attributes["stream_encrypted"] is True
    assert hass.states.get(camera).attributes["supported_features"] == 0
    await entry.runtime_data.async_run_audit()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # Switched off on the device: followed at the next refresh, without a reload.
    original = (FIXTURES / "nvr" / "ISAPI_System_Network_EZVIZ.xml").read_text()
    device.overrides[CLOUD_PATH] = (
        200,
        original.replace(
            "<streamEncrypteEnabled>true</streamEncrypteEnabled>",
            "<streamEncrypteEnabled>false</streamEncrypteEnabled>",
        ),
    )
    refresher = entry.runtime_data.refresher
    refresher.request_full()
    await refresher.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(sensor).state == STATE_OFF
    assert hass.states.get(camera).attributes["stream_encrypted"] is False
    assert hass.states.get(camera).attributes["supported_features"] == CameraEntityFeature.STREAM
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert {method for method, _ in device.requests} == {"GET"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_try_encrypted_stream_unblocks_camera_and_snapshots_use_resolution(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device, options={OPT_TRY_ENCRYPTED_STREAM: True})
    await _set_up_entry(hass, entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    camera = next(e.entity_id for e in entities if e.unique_id.endswith("_stream_1"))

    assert hass.states.get(camera).attributes["stream_encrypted"] is True
    assert hass.states.get(camera).attributes["supported_features"] == CameraEntityFeature.STREAM

    camera_obj = hass.data["camera"].get_entity(camera)
    img = await camera_obj.async_camera_image()
    assert img == b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    assert any("videoResolutionWidth=" in path for _, path in device.requests)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_snapshot_resolution_unsupported_falls_back_to_plain_picture(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    res_path = "/ISAPI/ContentMgmt/StreamingProxy/channels/101/picture?videoResolutionWidth=3840&videoResolutionHeight=2160"
    device.overrides[res_path] = (
        404,
        "<ResponseStatus><statusCode>4</statusCode><subStatusCode>notSupport</subStatusCode></ResponseStatus>",
    )
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    camera = next(e.entity_id for e in entities if e.unique_id.endswith("_stream_1"))

    camera_obj = hass.data["camera"].get_entity(camera)
    img = await camera_obj.async_camera_image()
    assert img == b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    bridge = entry.runtime_data
    assert "/ISAPI/ContentMgmt/StreamingProxy/channels/101/picture" not in bridge.link.unsupported
    assert res_path in bridge.link.unsupported

    device.requests.clear()
    img2 = await camera_obj.async_camera_image()
    assert img2 == b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    assert not any("videoResolutionWidth=" in path for _, path in device.requests)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_snapshot_resolution_rejected_falls_back_to_plain_picture(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    res_path = "/ISAPI/ContentMgmt/StreamingProxy/channels/101/picture?videoResolutionWidth=3840&videoResolutionHeight=2160"
    device.overrides[res_path] = (
        500,
        "<ResponseStatus><statusCode>2</statusCode><subStatusCode>deviceError</subStatusCode></ResponseStatus>",
    )
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    camera = next(e.entity_id for e in entities if e.unique_id.endswith("_stream_1"))

    camera_obj = hass.data["camera"].get_entity(camera)
    img = await camera_obj.async_camera_image()
    assert img == b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_leak_nothing(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result, default=str)
    for secret in (
        device.password,
        device.username,
        "TESTSERIAL101",
        "TESTSERIAL201",
        "02:00:00:00:01:01",
    ):
        assert secret not in text
    assert "profile" in result

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_wrong_password_starts_reauth_after_one_retry(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device, password="not-the-password")
    await _set_up_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    # Stored credentials get one retry with a fresh challenge, then reauth; no setup retries.
    assert device.failed_logins == 2
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_locked_device_is_not_hammered(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr", lock_after=0)
    server = await serve(device)
    entry = device_entry(server, device)
    await _set_up_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(device.requests) == 1


async def test_other_device_at_address_is_refused(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g3")
    server = await serve(device)
    entry = device_entry(server, device, serial="TESTSERIAL101")
    await _set_up_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_unreachable_device_retries_later(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    await server.close()
    await _set_up_entry(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
