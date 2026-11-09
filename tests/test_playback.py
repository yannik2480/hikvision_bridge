"""Recordings in the media browser, the download proxy and the recording actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import stat
from types import MappingProxyType

from aiohttp import web
from homeassistant.components import media_source
from homeassistant.components.media_player import BrowseError
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hikvision_bridge import media_source as media_source_module
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
from custom_components.hikvision_bridge.views import decode_token, encode_token, recording_url
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


async def browse(hass: HomeAssistant, identifier: str = ""):
    uri = f"media-source://{DOMAIN}/{identifier}".rstrip("/")
    return await media_source.async_browse_media(hass, uri)


def titles(folder) -> list[str]:
    return [c.title for c in folder.children]


async def test_browse_and_play(hass: HomeAssistant, serve, hass_client, fake_ffmpeg: Path) -> None:
    device, entry = await _setup(hass, serve, program_stream())
    # A camera: its one device is the channel, played from its memory card.
    device_id = entry.runtime_data.device_ids["1"]
    root = await browse(hass)
    assert [c.media_content_id for c in root.children] == [f"media-source://{DOMAIN}/{device_id}"]
    days = await browse(hass, device_id)
    today = dt_util.now().date().isoformat()
    assert [c.media_content_id for c in days.children] == [
        f"media-source://{DOMAIN}/{device_id}/{today}"
    ]
    # Two kinds on this day: every clip, newest first and its kind in front, and a folder per kind.
    day = await browse(hass, f"{device_id}/{today}")
    assert titles(day) == [
        "Continuous",
        "Motion",
        "Motion · 10:30:00 - 10:31:00",
        "Continuous · 10:00:00 - 10:10:00",
    ]
    assert [c.can_expand for c in day.children] == [True, True, False, False]
    motion = await browse(hass, f"{device_id}/{today}/motion")
    assert titles(motion) == ["10:30:00 - 10:31:00"]
    clip = motion.children[0]
    assert clip.media_content_id == day.children[2].media_content_id
    assert clip.media_content_id.startswith(f"media-source://{DOMAIN}/{device_id}/clip/")
    assert clip.can_play
    assert clip.thumbnail.startswith(f"/api/hikvision_bridge/thumbnail/{entry.entry_id}/1/card/")

    played = await media_source.async_resolve_media(hass, clip.media_content_id, None)
    assert played.mime_type == "video/mp4"
    assert f"/api/hikvision_bridge/recording/{entry.entry_id}/1/card/" in played.url

    client = await hass_client()
    path = played.url.split("?", 1)[0]
    converted = await client.get(path)
    assert converted.status == 200
    # The fake ffmpeg passes through what it gets: the stream from the device header on.
    assert await converted.read() == recordings.for_ffmpeg(program_stream())
    original = await client.get(path.replace("/mp4/", "/original/"))
    assert await original.read() == program_stream()
    download = next(w for w in device.writes if w[1] == recordings.DOWNLOAD_PATH)
    assert "<downloadRequest><playbackURI>rtsp://" in download[2]
    assert "&amp;name=b&amp;" in download[2]
    assert (await client.get(path.replace("/mp4/", "/bogus/"))).status == 404


def clipping(files: list[tuple[datetime, datetime, str, str]], *, clipped: bool = True):
    """Search answers with either clipped file times or full file times."""

    def respond(_method: str, body: str) -> web.Response:
        start, end = map(recordings.parse_time, re.findall(r"<(?:start|end)Time>([^<]+)", body))
        items = [
            match(
                (max(s, start) if clipped else s).astimezone(UTC),
                (min(e, end) - max(s, start) if clipped else e - s).total_seconds() / 60,
                kind,
                name,
            )
            for s, e, kind, name in files
            if s < end and e > start
        ]
        return web.Response(text=result(items, "OK"), content_type="application/xml")

    return respond


async def test_channels_of_every_device(hass: HomeAssistant, serve) -> None:
    """The root lists the channels of all devices, flat, by their device id."""
    _, camera_entry = await _setup(hass, serve, program_stream())
    recorder = FakeDevice("nvr")
    recorded(recorder, program_stream())
    entry = device_entry(await serve(recorder), recorder)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    devices = entry.runtime_data.device_ids
    channels = [c for c in entry.runtime_data.profile.channels if c in devices]
    root = await browse(hass)
    assert [c.media_content_id.rsplit("/", 1)[-1] for c in root.children] == [
        camera_entry.runtime_data.device_ids["1"],
        *(devices[c] for c in channels),
    ]
    assert titles(root)[1:] == [entry.runtime_data.profile.channels[c].name for c in channels]
    # The recorder's channels are devices of their own, under the recorder's.
    assert len(set(root.children)) == len(root.children)
    assert devices[None] not in [c.media_content_id.rsplit("/", 1)[-1] for c in root.children]

    # Anything but a channel device of this integration is refused.
    unknown = [devices[None], "0123456789abcdef", ""]
    for device_id in unknown:
        with pytest.raises(BrowseError):
            await browse(hass, f"{device_id}/2026-10-21")
        with pytest.raises(media_source.Unresolvable):
            await media_source.async_resolve_media(
                hass, f"media-source://{DOMAIN}/{device_id}/clip/{encode_token('rtsp://x')}", None
            )
    channel = devices[channels[0]]
    for bad in ("nope", "2026-10-21/all", "2026-10-21/motion/x", "clip"):
        with pytest.raises(BrowseError):
            await browse(hass, f"{channel}/{bad}")


async def test_days(hass: HomeAssistant, serve) -> None:
    device, entry = await _setup(hass, serve, program_stream())
    device_id = entry.runtime_data.device_ids["1"]
    today = dt_util.now().date()
    yesterday = today - timedelta(days=1)

    def calendar(_method: str, body: str) -> web.Response:
        listed = "".join(
            f"<day><dayOfMonth>{d.day}</dayOfMonth><record>true</record></day>"
            for d in (yesterday, today)
            if f"<monthOfYear>{d.month}</monthOfYear>" in body
        )
        return web.Response(
            text=f"<trackDailyDistribution><dayList>{listed}</dayList></trackDailyDistribution>",
            content_type="application/xml",
        )

    device.responders["/ISAPI/ContentMgmt/record/tracks/101/dailyDistribution"] = calendar
    days = await browse(hass, device_id)
    assert titles(days) == [today.isoformat(), yesterday.isoformat()]

    # Without a calendar, the last days.
    device.responders["/ISAPI/ContentMgmt/record/tracks/101/dailyDistribution"] = lambda _m, _b: (
        web.Response(status=404)
    )
    days = await browse(hass, device_id)
    assert len(days.children) == media_source_module.DAYS_WITHOUT_CALENDAR
    assert days.children[0].title == today.isoformat()
    assert days.children[1].title == yesterday.isoformat()


async def test_files_of_a_day(hass: HomeAssistant, serve) -> None:
    """Files that run past midnight are listed once, on their day, with their real times."""
    device, entry = await _setup(hass, serve, program_stream())
    device_id = entry.runtime_data.device_ids["1"]
    midnight = dt_util.start_of_local_day(NOW)
    files = [
        (midnight - timedelta(hours=5), midnight - timedelta(hours=2), "timing", "a"),
        (midnight - timedelta(hours=2), midnight + timedelta(minutes=50), "timing", "b"),
        (midnight + timedelta(minutes=50), NOW - timedelta(minutes=5), "timing", "c"),
        # Still recording: the device lists it up to the end of the search.
        (NOW - timedelta(minutes=5), midnight + timedelta(days=1), "timing", "d"),
    ]
    respond = clipping(files)
    spans: list[timedelta] = []

    def search(method: str, body: str) -> web.Response:
        start, end = map(recordings.parse_time, re.findall(r"<(?:start|end)Time>([^<]+)", body))
        spans.append(end - start)
        return respond(method, body)

    device.responders[recordings.SEARCH_PATH] = search
    yesterday = await browse(hass, f"{device_id}/{(midnight - timedelta(days=1)).date()}")
    # One kind: the files only, without it.
    assert titles(yesterday) == ["22:00:00 - 00:50:00", "19:00:00 - 22:00:00"]
    # The file past midnight ends within the hour: no whole day more is searched for it.
    assert spans == [timedelta(days=1, seconds=1), timedelta(hours=3)]
    uri = decode_token(yesterday.children[0].media_content_id.rsplit("/", 1)[-1])
    assert f"&endtime={recordings.compact(midnight + timedelta(minutes=50))}&" in uri
    today = await browse(hass, f"{device_id}/{midnight.date()}")
    assert titles(today) == ["11:55:00 - 12:00:00", "00:50:00 - 11:55:00"]

    # A file running past that hour is searched on for a day.
    files[1:3] = [
        (midnight - timedelta(hours=2), midnight + timedelta(hours=3), "timing", "b"),
        (midnight + timedelta(hours=3), NOW - timedelta(minutes=5), "timing", "c"),
    ]
    yesterday = await browse(hass, f"{device_id}/{(midnight - timedelta(days=1)).date()}")
    assert titles(yesterday) == ["22:00:00 - 03:00:00", "19:00:00 - 22:00:00"]
    assert spans[-2:] == [timedelta(hours=3), timedelta(days=1, hours=2)]


async def test_h264_playback_option(hass: HomeAssistant, serve) -> None:
    _, entry = await _setup(hass, serve, program_stream(), **{OPT_PLAYBACK: "h264"})
    token = "cnRzcDovL3gvU3RyZWFtaW5nL3RyYWNrcy8xMDEvP3N0YXJ0dGltZT0x"
    device_id = entry.runtime_data.device_ids["1"]
    played = await media_source.async_resolve_media(
        hass, f"media-source://{DOMAIN}/{device_id}/clip/{token}", None
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
        assert await answer.read() == recordings.for_ffmpeg(recording(encrypted=False))


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
    assert content == recordings.for_ffmpeg(program_stream())
    assert not list(saved.parent.glob(".*.part"))

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "download_recording",
            {"device_id": _device_id(hass, entry), "start": NOW, "end": START},
            blocking=True,
        )
    assert err.value.translation_key == "bad_period"


async def test_files_over_the_period_ends(
    hass: HomeAssistant, serve, tmp_path: Path, fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files running over the ends of the period have their real times, and are trimmed so."""
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    device, entry = await _setup(hass, serve, program_stream())
    files = [
        (START - timedelta(hours=1), START + timedelta(minutes=30), "timing", "a"),
        (START + timedelta(minutes=30), START + timedelta(minutes=90), "timing", "b"),
    ]
    respond = clipping(files)
    searches: list[tuple[datetime, datetime]] = []

    def search(method: str, body: str) -> web.Response:
        start, end = map(recordings.parse_time, re.findall(r"<(?:start|end)Time>([^<]+)", body))
        searches.append((start, end))
        return respond(method, body)

    device.responders[recordings.SEARCH_PATH] = search
    start, end = START + timedelta(minutes=10), START + timedelta(hours=1)
    expected_searches = [
        (start, end),
        (start - timedelta(hours=1), end + timedelta(hours=1)),
        (start - timedelta(days=1), end + timedelta(hours=1)),
    ]
    period = {
        "device_id": _device_id(hass, entry),
        "start": start,
        "end": end,
    }
    found = await hass.services.async_call(
        DOMAIN, "search_recordings", period, blocking=True, return_response=True
    )
    assert [(r["start"], r["end"]) for r in found["recordings"]] == [
        (s.isoformat(), e.isoformat()) for s, e, _, _ in files
    ]
    assert searches == expected_searches
    searches.clear()
    trims = []
    ffmpeg_args = playback.ffmpeg_args

    def recorded_args(*args, **kwargs):
        trims.append((kwargs["offset"], kwargs["duration"]))
        return ffmpeg_args(*args, **kwargs)

    monkeypatch.setattr(playback, "ffmpeg_args", recorded_args)
    await hass.services.async_call(
        DOMAIN, "download_recording", period, blocking=True, return_response=True
    )
    # From 70 minutes into the first file on; the first 30 minutes of the second.
    assert trims == [(4200.0, None), (None, 1800.0)]
    assert searches == expected_searches

    files[:] = [(START + timedelta(minutes=20), START + timedelta(minutes=150), "timing", "c")]
    searches.clear()
    right_searches = [
        (start, end),
        (start, end + timedelta(hours=1)),
        (start, end + timedelta(days=1)),
    ]
    found = await hass.services.async_call(
        DOMAIN, "search_recordings", period, blocking=True, return_response=True
    )
    assert [(r["start"], r["end"]) for r in found["recordings"]] == [
        (s.isoformat(), e.isoformat()) for s, e, _, _ in files
    ]
    assert searches == right_searches

    files[:] = [
        (START - timedelta(hours=1), START + timedelta(minutes=30), "timing", "a"),
        (START + timedelta(minutes=30), START + timedelta(minutes=90), "timing", "b"),
    ]
    respond = clipping(files, clipped=False)
    searches.clear()
    full_time_searches = [(start, end)]
    found = await hass.services.async_call(
        DOMAIN, "search_recordings", period, blocking=True, return_response=True
    )
    assert [(r["start"], r["end"]) for r in found["recordings"]] == [
        (s.isoformat(), e.isoformat()) for s, e, _, _ in files
    ]
    assert searches == full_time_searches

    start += timedelta(microseconds=500_000)
    period["start"] = start
    files[:] = [(START + timedelta(minutes=5), START + timedelta(minutes=40), "timing", "c")]
    respond = clipping(files)
    searches.clear()
    found = await hass.services.async_call(
        DOMAIN, "search_recordings", period, blocking=True, return_response=True
    )
    assert found["recordings"][0]["start"] == files[0][0].isoformat()
    assert searches == [
        (start.replace(microsecond=0), end),
        (start.replace(microsecond=0) - timedelta(hours=1), end),
    ]


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

    device_id = entry.runtime_data.device_ids["1"]
    # The media browser plays from the recorder; the camera's card is in the panel.
    day = await browse(hass, f"{device_id}/{dt_util.now().date()}")
    assert len(day.children) == 4
    assert "/1/recorder/" in day.children[-1].thumbnail
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
