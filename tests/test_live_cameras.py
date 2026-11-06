"""Camera stream sources use the local decrypting relay when a code is configured."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hikvision_bridge.const import OPT_STREAM_KEY, OPT_TRY_ENCRYPTED_STREAM
from custom_components.hikvision_bridge.model.discover import CLOUD_PATH
from tests.conftest import device_entry
from tests.fakedevice import FIXTURES, FakeDevice


@pytest.mark.parametrize("options", [{OPT_STREAM_KEY: "ABCDEF"}, {OPT_TRY_ENCRYPTED_STREAM: True}])
async def test_saved_or_device_code_enables_loopback_stream(
    hass: HomeAssistant, serve, options
) -> None:
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
