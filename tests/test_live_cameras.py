"""Camera stream sources use the local decrypting relay when a code is configured."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.hikvision_bridge.const import OPT_STREAM_KEY, OPT_TRY_ENCRYPTED_STREAM
from custom_components.hikvision_bridge.model.discover import CLOUD_PATH
from tests.conftest import device_entry
from tests.fakedevice import FIXTURES, FakeDevice


async def test_saved_code_enables_known_h264_loopback_stream(hass: HomeAssistant, serve) -> None:
    options = {OPT_STREAM_KEY: "ABCDEF"}
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device, options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cameras = list(hass.data["camera"].entities)
    camera = next(
        e
        for e in cameras
        if e.bridge.entry.entry_id == entry.entry_id and e.channel_id == "1" and e._stream.rank == 1
    )
    camera._stream.codec = "H.264"
    assert camera.supported_features & CameraEntityFeature.STREAM
    url = await camera.stream_source()
    assert url is not None
    parts = urlsplit(url)
    assert parts.hostname == "127.0.0.1"
    assert parts.username is None
    assert parts.password is None
    assert "ABCDEF" not in url
    assert "/Streaming/Channels/" not in parts.path
    assert parts.port is not None
    # Source queries share one listener instead of allocating a new port each time.
    assert await camera.stream_source() == url
    assert camera.stream_options["rtsp_transport"] == "tcp"
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    try:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def test_saved_recording_code_leaves_clear_live_stream_unchanged(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    original = (FIXTURES / "nvr" / "ISAPI_System_Network_EZVIZ.xml").read_text()
    device.overrides[CLOUD_PATH] = (
        200,
        original.replace(
            "<streamEncrypteEnabled>true</streamEncrypteEnabled>",
            "<streamEncrypteEnabled>false</streamEncrypteEnabled>",
        ),
    )
    server = await serve(device)
    entry = device_entry(server, device, {OPT_STREAM_KEY: "ABCDEF"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    camera = next(
        e
        for e in hass.data["camera"].entities
        if e.bridge.entry.entry_id == entry.entry_id and e.channel_id == "1" and e._stream.rank == 1
    )
    source = urlsplit(await camera.stream_source())
    assert source.username == device.username
    assert source.path == "/Streaming/Channels/101"
    assert not entry.runtime_data.live_relays
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("options", "codec", "missing_cloud"),
    [
        ({OPT_TRY_ENCRYPTED_STREAM: True}, "H.265", False),
        ({OPT_TRY_ENCRYPTED_STREAM: True}, "H.264", False),
        ({OPT_STREAM_KEY: "ABCDEF", OPT_TRY_ENCRYPTED_STREAM: True}, "H.265", False),
        ({OPT_STREAM_KEY: "ABCDEF"}, "H.265", True),
        ({OPT_STREAM_KEY: "ABCDEF"}, None, True),
    ],
)
async def test_recording_code_and_live_override_preserve_direct_source(
    hass: HomeAssistant,
    serve,
    options,
    codec,
    missing_cloud,
) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device, options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    camera = next(
        e
        for e in hass.data["camera"].entities
        if e.bridge.entry.entry_id == entry.entry_id and e.channel_id == "1" and e._stream.rank == 1
    )
    camera._stream.codec = codec
    if missing_cloud:
        entry.runtime_data.profile.cloud = None
    before = len(device.requests)
    try:
        source = urlsplit(await camera.stream_source())
        assert source.username == device.username
        assert source.path == "/Streaming/Channels/101"
        assert not entry.runtime_data.live_relays
        assert len(device.requests) == before  # Override alone must not fetch a decryption code.
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_rejected_relay_raises_repair_and_stops_offering_stream(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device, {OPT_STREAM_KEY: "ABCDEF"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    camera = next(
        e
        for e in hass.data["camera"].entities
        if e.bridge.entry.entry_id == entry.entry_id and e.channel_id == "1" and e._stream.rank == 1
    )
    camera._stream.codec = "H.264"
    issue_id = f"{entry.entry_id}_live_decryption"
    try:
        assert await camera.stream_source() is not None
        relay = entry.runtime_data.live_relays["101"]
        relay.failure = "rejected"
        relay.on_failure()
        issue = ir.async_get(hass).async_get_issue("hikvision_bridge", issue_id)
        assert issue is not None
        assert issue.translation_key == "live_decryption_failed"
        assert await camera.stream_source() is None
        # A new relay (as after a reload) starts clean and removes the repair.
        entry.runtime_data.live_relays.clear()
        assert await camera.stream_source() is not None
        assert ir.async_get(hass).async_get_issue("hikvision_bridge", issue_id) is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_saved_code_decrypts_when_dvr_omits_encryption_flag(
    hass: HomeAssistant, serve
) -> None:
    device = FakeDevice("nvr")
    missing = "<EZVIZ><enabled>true</enabled><registerStatus>true</registerStatus></EZVIZ>"
    device.overrides[CLOUD_PATH] = (200, missing)
    server = await serve(device)
    entry = device_entry(server, device, {OPT_STREAM_KEY: "ABCDEF"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    camera = next(
        e
        for e in hass.data["camera"].entities
        if e.bridge.entry.entry_id == entry.entry_id and e.channel_id == "1" and e._stream.rank == 2
    )
    try:
        assert camera._stream.codec == "H.264"
        assert entry.runtime_data.profile.cloud.stream_encrypted is None
        url = await camera.stream_source()
        assert urlsplit(url).username is None
        assert "/Streaming/Channels/" not in url
        # An explicit off flag still preserves the original direct source after refresh.
        device.overrides[CLOUD_PATH] = (
            200,
            missing.replace(
                "</EZVIZ>", "<streamEncryptEnabled>false</streamEncryptEnabled></EZVIZ>"
            ),
        )
        entry.runtime_data.refresher.request_full()
        await entry.runtime_data.refresher.async_refresh()
        direct = urlsplit(await camera.stream_source())
        assert direct.username == device.username
        assert direct.path == "/Streaming/Channels/102"
        # Missing again must not be remembered as an explicit false.
        device.overrides[CLOUD_PATH] = (200, missing)
        entry.runtime_data.refresher.request_full()
        await entry.runtime_data.refresher.async_refresh()
        assert await camera.stream_source() == url
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
