"""Recordings in the media browser, the download proxy and the recording actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import stat
from types import MappingProxyType

from aiohttp import web
from homeassistant.components import media_source
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hikvision_bridge.const import (
    CONF_CHANNEL_ID,
    CONF_EVENT_SOURCE,
    CONF_SCHEME,
    CONF_VERIFY_TLS,
    DOMAIN,
    EVENT_SOURCE_RECORDER,
    OPT_PLAYBACK,
    OPT_STREAM_KEY,
    SUBENTRY_CHANNEL_LINK,
)
from custom_components.hikvision_bridge.model.discover import CLOUD_PATH
from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.runtime import playback
from custom_components.hikvision_bridge.views import recording_url
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice
from tests.model.test_recordings import match, program_stream, result, searcher
from tests.model.test_scrambling import CODE, recording

# Noon in the US/Pacific test time zone, so recordings two hours earlier are still "today".
NOW = datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
START = NOW - timedelta(hours=2)
CLIPS = [match(START, 10, "timing", "a"), match(START + timedelta(minutes=30), 1, "motion", "b")]


@pytest.fixture(autouse=True)
def frozen_clock(freezer) -> None:
    """Run every test at NOW."""
    freezer.move_to(NOW)


def recorded(device: FakeDevice, data: bytes) -> None:
    device.responders[recordings.SEARCH_PATH] = searcher(CLIPS, 10)
    device.responders[recordings.DOWNLOAD_PATH] = lambda _m, _b: web.Response(
        body=data, content_type="application/binary"
    )
    device.responders["/ISAPI/ContentMgmt/record/tracks/101/dailyDistribution"] = days


def ezviz(code: str):
    """Platform access settings that report a verification code."""
    text = (
        '<EZVIZ version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        f"<enabled>true</enabled><verificationCode>{code}</verificationCode></EZVIZ>"
    )
    return lambda _m, _b: web.Response(text=text, content_type="application/xml")


def days(_method: str, body: str) -> web.Response:
    """Only today has recordings."""
    today = dt_util.now()
    listed = f"<monthOfYear>{today.month}</monthOfYear>" in body
    day = (
        f"<day><id>1</id><dayOfMonth>{today.day}</dayOfMonth><record>true</record></day>"
        if listed
        else ""
    )
    return web.Response(
        text=f"<trackDailyDistribution><dayList>{day}</dayList></trackDailyDistribution>",
        content_type="application/xml",
    )


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An 'ffmpeg' that passes its input through; it writes to a file target if given."""
    script = tmp_path / "ffmpeg"
    script.write_text(
        '#!/bin/sh\nfor last; do :; done\nif [ "$last" = "pipe:1" ]; then cat; '
        'else cat > "$last"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(playback, "ffmpeg_binary", lambda _hass: str(script))
    return script


async def _setup(hass: HomeAssistant, serve, data: bytes, **options):
    device = FakeDevice("camera_g3")
    recorded(device, data)
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await async_setup_component(hass, "media_source", {})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return device, entry


async def test_browse_and_play(hass: HomeAssistant, serve, hass_client, fake_ffmpeg: Path) -> None:
    device, entry = await _setup(hass, serve, program_stream())
    root = await media_source.async_browse_media(hass, f"media-source://{DOMAIN}")
    assert [c.title for c in root.children] == [entry.title]
    channels = await media_source.async_browse_media(hass, root.children[0].media_content_id)
    assert len(channels.children) == 1
    # One source only (the memory card): the channel lists days directly.
    days = await media_source.async_browse_media(hass, channels.children[0].media_content_id)
    assert len(days.children) == 1
    day = await media_source.async_browse_media(hass, days.children[0].media_content_id)
    # Two kinds on this day: a folder for all and one per kind.
    assert [c.media_content_id.rsplit("/", 1)[-1] for c in day.children] == [
        "all",
        "continuous",
        "motion",
    ]
    motion = await media_source.async_browse_media(hass, day.children[2].media_content_id)
    assert len(motion.children) == 1
    clip = motion.children[0]
    assert clip.can_play
    assert clip.thumbnail.startswith("/api/hikvision_bridge/thumbnail/")

    played = await media_source.async_resolve_media(hass, clip.media_content_id, None)
    assert played.mime_type == "video/mp4"
    assert "/api/hikvision_bridge/recording/" in played.url

    client = await hass_client()
    path = played.url.split("?", 1)[0]
    converted = await client.get(path)
    assert converted.status == 200
    # The fake ffmpeg passes through what it gets: the stream without the device header.
    assert await converted.read() == recordings.strip_header(program_stream())
    original = await client.get(path.replace("/mp4/", "/original/"))
    assert await original.read() == program_stream()
    download = next(w for w in device.writes if w[1] == recordings.DOWNLOAD_PATH)
    assert "<downloadRequest><playbackURI>rtsp://" in download[2]
    assert "&amp;name=b&amp;" in download[2]
    assert (await client.get(path.replace("/mp4/", "/bogus/"))).status == 404


async def test_h264_playback_option(hass: HomeAssistant, serve) -> None:
    _, entry = await _setup(hass, serve, program_stream(), **{OPT_PLAYBACK: "h264"})
    token = "cnRzcDovL3gvU3RyZWFtaW5nL3RyYWNrcy8xMDEvP3N0YXJ0dGltZT0x"
    played = await media_source.async_resolve_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/1/card/clip/{token}", None
    )
    assert "/h264/" in played.url


async def test_encrypted_recording(hass: HomeAssistant, serve, hass_client, tmp_path: Path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    _, entry = await _setup(hass, serve, program_stream(scrambled=True))
    found = await hass.services.async_call(
        DOMAIN,
        "search_recordings",
        {
            "device_id": _device_id(hass, entry),
            "start": START - timedelta(minutes=5),
            "end": NOW,
        },
        blocking=True,
        return_response=True,
    )
    uri = recordings.parse_results(parse(result(CLIPS, "OK")), "101")[0][0].playback_uri
    client = await hass_client()
    answer = await client.get(recording_url(entry.entry_id, "1", "card", "original", uri))
    assert answer.status == 409
    assert [r["kind"] for r in found["recordings"]] == ["continuous", "motion"]
    assert found["source"] == "card"
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "download_recording",
            {"device_id": _device_id(hass, entry), "start": START, "end": NOW},
            blocking=True,
        )
    assert err.value.translation_key == "recording_encrypted"


@pytest.mark.parametrize(
    ("device_code", "options", "status"),
    [
        (CODE, {}, 200),  # read from the device
        ("", {OPT_STREAM_KEY: CODE}, 200),  # cameras leave it blank; entered in the options
        ("WRONG1", {OPT_STREAM_KEY: "WRONG2"}, 409),
    ],
)
async def test_encrypted_recording_is_decrypted(
    hass: HomeAssistant,
    serve,
    hass_client,
    fake_ffmpeg: Path,
    device_code: str,
    options: dict,
    status: int,
) -> None:
    device, entry = await _setup(hass, serve, recording(encrypted=True), **options)
    device.responders[CLOUD_PATH] = ezviz(device_code)
    uri = recordings.parse_results(parse(result(CLIPS, "OK")), "101")[0][0].playback_uri
    client = await hass_client()
    answer = await client.get(recording_url(entry.entry_id, "1", "card", "mp4", uri))
    assert answer.status == status
    if status == 200:
        assert await answer.read() == recordings.strip_header(recording(encrypted=False))


async def test_download_recording(
    hass: HomeAssistant, serve, tmp_path: Path, fake_ffmpeg: Path
) -> None:
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    _, entry = await _setup(hass, serve, program_stream())
    answer = await hass.services.async_call(
        DOMAIN,
        "download_recording",
        {
            "device_id": _device_id(hass, entry),
            "start": START,
            "end": NOW,
            "format": "original",
        },
        blocking=True,
        return_response=True,
    )
    files = [Path(f) for f in answer["files"]]
    assert len(files) == 2
    contents = [await hass.async_add_executor_job(f.read_bytes) for f in files]
    assert all(f.suffix == ".ps" for f in files)
    assert contents == [program_stream()] * 2
    assert files[0].parent.parent == tmp_path / "media" / DOMAIN

    answer = await hass.services.async_call(
        DOMAIN,
        "download_recording",
        {
            "device_id": _device_id(hass, entry),
            "start": START + timedelta(minutes=29),
            "end": NOW,
        },
        blocking=True,
        return_response=True,
    )
    saved = Path(answer["files"][0])
    assert saved.suffix == ".mp4"
    content = await hass.async_add_executor_job(saved.read_bytes)
    assert content == recordings.strip_header(program_stream())
    assert not list(saved.parent.glob(".*.part"))

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "download_recording",
            {"device_id": _device_id(hass, entry), "start": NOW, "end": START},
            blocking=True,
        )
    assert err.value.translation_key == "bad_period"


def _device_id(hass: HomeAssistant, entry) -> str:
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)[0].id


async def test_recorder_or_memory_card(hass: HomeAssistant, serve) -> None:
    """A recorder channel with a direct camera link offers both stores."""
    recorder = FakeDevice("nvr")
    recorded(recorder, program_stream())
    camera = FakeDevice("camera_g3", password="camera-own-pass")
    camera_clips = [match(START + timedelta(minutes=40), 1, "motion", "card1")]
    camera.responders[recordings.SEARCH_PATH] = searcher(camera_clips, 10)
    recorder_server = await serve(recorder)
    camera_server = await serve(camera)
    entry = device_entry(recorder_server, recorder)
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {
                    CONF_HOST: camera_server.host,
                    CONF_PORT: camera_server.port,
                    CONF_SCHEME: "http",
                    CONF_VERIFY_TLS: True,
                    CONF_USERNAME: camera.username,
                    CONF_PASSWORD: camera.password,
                    CONF_CHANNEL_ID: "1",
                    CONF_EVENT_SOURCE: EVENT_SOURCE_RECORDER,
                }
            ),
            subentry_type=SUBENTRY_CHANNEL_LINK,
            title="Camera (1)",
            unique_id="1",
        ),
    )
    assert await async_setup_component(hass, "media_source", {})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    channel = await media_source.async_browse_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/1"
    )
    assert [c.media_content_id.rsplit("/", 1)[-1] for c in channel.children] == [
        "recorder",
        "card",
    ]
    device_id = entry.runtime_data.device_ids["1"]
    period = {"device_id": device_id, "start": START, "end": NOW}
    from_card = await hass.services.async_call(
        DOMAIN,
        "search_recordings",
        {**period, "source": "card"},
        blocking=True,
        return_response=True,
    )
    assert from_card["source"] == "card"
    assert len(from_card["recordings"]) == 1
    automatic = await hass.services.async_call(
        DOMAIN, "search_recordings", period, blocking=True, return_response=True
    )
    assert automatic["source"] == "recorder"
    assert len(automatic["recordings"]) == 2

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
