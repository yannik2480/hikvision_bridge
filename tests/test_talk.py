"""Talking through a device's speaker: discovery, the raw upload, the session and the websocket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hikvision_bridge.const import DOMAIN
from custom_components.hikvision_bridge.model.features.talk import TalkChannel, parse_channels
from custom_components.hikvision_bridge.runtime import talk
from custom_components.hikvision_bridge.runtime.bridge import DeviceBridge
from custom_components.hikvision_bridge.wire.faults import BadCredentials, Unreachable
from custom_components.hikvision_bridge.wire.link import UPLOAD_BACKLOG, Endpoint, IsapiLink
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice
from tests.talkdevice import OK, TalkDevice

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'
CHANNELS = (
    f'<TwoWayAudioChannelList version="2.0" {NS}>'
    "<TwoWayAudioChannel><id>1</id><enabled>true</enabled>"
    "<audioCompressionType>G.711ulaw</audioCompressionType></TwoWayAudioChannel>"
    "</TwoWayAudioChannelList>"
)
CHANNEL = TalkChannel("1", "G.711ulaw")
FRAME = bytes(range(160))


def test_the_first_g711_channel_is_used() -> None:
    assert parse_channels(parse(CHANNELS)) == CHANNEL
    assert CHANNEL.path == "/ISAPI/System/TwoWayAudio/channels/1"
    other = CHANNELS.replace("G.711ulaw", "G.726")
    assert parse_channels(parse(other)) is None
    # Devices report the channel disabled while nobody talks: it is used all the same.
    idle = CHANNELS.replace("<enabled>true", "<enabled>false")
    assert parse_channels(parse(idle)) == CHANNEL
    assert parse_channels(None) is None


@pytest.fixture
async def device(socket_enabled: None) -> AsyncIterator[TalkDevice]:
    talker = TalkDevice()
    yield talker
    await talker.close()


@pytest.fixture
async def link(device: TalkDevice) -> AsyncIterator[IsapiLink]:
    host, port = await device.start()
    async with aiohttp.ClientSession() as session:
        yield IsapiLink(session, Endpoint(host, port), device.username, device.password)


async def _started(link: IsapiLink, ends: list[talk.TalkEnd]) -> talk.Talk:
    return await talk.TalkManager().start("entry", link, CHANNEL, ends.append)


async def test_the_microphone_reaches_the_device(device: TalkDevice, link: IsapiLink) -> None:
    ends: list[talk.TalkEnd] = []
    session = await _started(link, ends)
    assert session.codec == "G.711ulaw"
    for _ in range(3):
        session.send(FRAME)
    await asyncio.sleep(0.05)
    await session.close(talk.TalkEnd.STOPPED)
    await asyncio.wait_for(device.uploads_closed.wait(), 2)

    assert bytes(device.audio) == FRAME * 3
    assert session.sent == len(FRAME) * 3
    assert ends == [talk.TalkEnd.STOPPED]
    # A previous talk may still hold the channel: closed first, and again at the end.
    path = CHANNEL.path
    signed = [r for r in device.requests if r[1].startswith(path)]
    assert signed[0] == ("PUT", f"{path}/close")
    assert ("PUT", f"{path}/open") in signed
    assert ("PUT", f"{path}/audioData") in signed
    assert signed[-1] == ("PUT", f"{path}/close")
    # Ending twice ends once.
    await session.close(talk.TalkEnd.STOPPED)
    assert ends == [talk.TalkEnd.STOPPED]
    session.send(FRAME)
    assert session.sent == len(FRAME) * 3


async def test_the_session_of_the_firmware_is_named(device: TalkDevice, link: IsapiLink) -> None:
    device.open_reply = f"<TwoWayAudioSession {NS}><sessionId>7</sessionId></TwoWayAudioSession>"
    session = await _started(link, [])
    assert ("PUT", f"{CHANNEL.path}/audioData?sessionId=7") in device.requests
    await session.close(talk.TalkEnd.STOPPED)


async def test_an_expired_nonce_is_renewed_for_the_upload(
    device: TalkDevice, link: IsapiLink
) -> None:
    device.refuse_upload = 1
    session = await _started(link, [])
    uploads = [r for r in device.requests if r[1].endswith("/audioData")]
    assert len(uploads) == 2
    await session.close(talk.TalkEnd.STOPPED)


async def test_the_device_ending_the_talk(device: TalkDevice, link: IsapiLink) -> None:
    device.hang_up_after = len(FRAME)
    ends: list[talk.TalkEnd] = []
    session = await _started(link, ends)
    session.send(FRAME)
    await asyncio.wait_for(device.uploads_closed.wait(), 2)
    for _ in range(20):
        if ends:
            break
        await asyncio.sleep(0.02)
    assert ends == [talk.TalkEnd.DEVICE]
    assert device.requests[-1] == ("PUT", f"{CHANNEL.path}/close")


async def test_another_talk_takes_over(device: TalkDevice, link: IsapiLink) -> None:
    manager = talk.TalkManager()
    first: list[talk.TalkEnd] = []
    second: list[talk.TalkEnd] = []
    one = await manager.start("entry", link, CHANNEL, first.append)
    two = await manager.start("entry", link, CHANNEL, second.append)
    assert first == [talk.TalkEnd.REPLACED]
    assert manager.active("entry") is two
    assert one is not two
    await manager.stop_entry("entry")
    assert second == [talk.TalkEnd.UNLOADED]
    assert manager.active("entry") is None
    await manager.stop_entry("entry")


async def test_a_refused_open_fails_and_frees_the_channel(
    device: TalkDevice, link: IsapiLink
) -> None:
    device.open_status = 403
    device.open_reply = "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>"
    with pytest.raises(talk.TalkError) as caught:
        await _started(link, [])
    assert caught.value.reason == "talk_failed"
    assert device.requests[-1] == ("PUT", f"{CHANNEL.path}/close")
    assert not any(r[1].endswith("/audioData") for r in device.requests)


async def test_wrong_credentials_never_reach_the_upload(device: TalkDevice) -> None:
    host, port = await device.start()
    async with aiohttp.ClientSession() as session:
        link = IsapiLink(session, Endpoint(host, port), device.username, "wrong")
        with pytest.raises(BadCredentials):
            await link.open_upload(f"{CHANNEL.path}/audioData")


async def test_an_unreachable_device(socket_enabled: None) -> None:
    async with aiohttp.ClientSession() as session:
        link = IsapiLink(session, Endpoint("127.0.0.1", 9), "u", "p", connect_timeout=1)
        link._auth_checked = True  # the upload is what fails here, not the probe
        with pytest.raises(Unreachable):
            await link.open_upload("/ISAPI/System/TwoWayAudio/channels/1/audioData")


async def test_frames_beyond_the_backlog_are_dropped(device: TalkDevice, link: IsapiLink) -> None:
    session = await _started(link, [])
    upload = session._upload
    assert upload is not None
    transport = upload._writer.transport
    real = transport.get_write_buffer_size
    transport.get_write_buffer_size = lambda: UPLOAD_BACKLOG + 1  # type: ignore[method-assign]
    session.send(FRAME)
    assert session.dropped == len(FRAME)
    transport.get_write_buffer_size = real  # type: ignore[method-assign]
    await session.close(talk.TalkEnd.STOPPED)


async def _talking_entry(hass: HomeAssistant, serve, device: TalkDevice):
    """An entry whose device lists a speaker; its link then points at the talking device."""
    fake = FakeDevice("camera_g2")
    fake.documents["/ISAPI/System/TwoWayAudio/channels"] = CHANNELS
    server = await serve(fake)
    entry = device_entry(server, fake)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bridge: DeviceBridge = entry.runtime_data
    assert bridge.profile.talk == CHANNEL
    host, port = await device.start()
    session = aiohttp.ClientSession()
    bridge.link = IsapiLink(session, Endpoint(host, port), device.username, device.password)
    return entry, session


async def test_talking_over_the_websocket(
    hass: HomeAssistant, serve, hass_ws_client, device: TalkDevice
) -> None:
    entry, session = await _talking_entry(hass, serve, device)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/cameras"})
    [camera] = (await client.receive_json())["result"]["cameras"]
    assert camera["talk"] is True

    await client.send_json_auto_id({"type": f"{DOMAIN}/talk", "entry_id": entry.entry_id})
    result = await client.receive_json()
    assert result["success"]
    started = (await client.receive_json())["event"]
    assert started["type"] == "started"
    assert started["codec"] == "G.711ulaw"
    await client.send_bytes(bytes([started["handler_id"]]) + FRAME)
    await client.send_bytes(bytes([started["handler_id"]]) + FRAME)
    for _ in range(50):
        if len(device.audio) >= 2 * len(FRAME):
            break
        await asyncio.sleep(0.02)
    assert bytes(device.audio) == FRAME * 2

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": result["id"]})
    assert (await client.receive_json())["success"]
    await asyncio.wait_for(device.uploads_closed.wait(), 2)
    await hass.async_block_till_done()
    assert talk.manager(hass).active(entry.entry_id) is None
    await session.close()


async def test_the_card_hears_when_the_talk_ends(
    hass: HomeAssistant, serve, hass_ws_client, device: TalkDevice
) -> None:
    entry, session = await _talking_entry(hass, serve, device)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/talk", "entry_id": entry.entry_id})
    await client.receive_json()
    await client.receive_json()

    # Another dashboard takes the device over.
    other = await hass_ws_client(hass)
    await other.send_json_auto_id({"type": f"{DOMAIN}/talk", "entry_id": entry.entry_id})
    ended = (await client.receive_json())["event"]
    assert ended == {"type": "ended", "reason": "replaced"}
    assert (await other.receive_json())["success"]

    # Unloading ends the talk too.
    await other.receive_json()
    await hass.config_entries.async_unload(entry.entry_id)
    assert (await other.receive_json())["event"] == {"type": "ended", "reason": "unloaded"}
    await session.close()


async def test_no_talk_without_a_speaker(hass: HomeAssistant, serve, hass_ws_client) -> None:
    fake = FakeDevice("camera_g2")
    server = await serve(fake)
    entry = device_entry(server, fake)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/cameras"})
    [camera] = (await client.receive_json())["result"]["cameras"]
    assert camera["talk"] is False
    await client.send_json_auto_id({"type": f"{DOMAIN}/talk", "entry_id": entry.entry_id})
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def test_a_device_refusing_the_talk(
    hass: HomeAssistant, serve, hass_ws_client, device: TalkDevice
) -> None:
    device.open_status = 403
    device.open_reply = OK
    entry, session = await _talking_entry(hass, serve, device)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/talk", "entry_id": entry.entry_id})
    assert (await client.receive_json())["error"]["code"] == "talk_failed"
    await session.close()
