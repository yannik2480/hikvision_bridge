"""Panel and card backend: camera list, timeline, playback sessions and their files."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import stat

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hikvision_bridge.const import DOMAIN
from custom_components.hikvision_bridge.model.discover import CLOUD_PATH
from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.model.features.scrambling import ProgramStream
from custom_components.hikvision_bridge.runtime import playback, sessions
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice
from tests.model.test_recordings import CLIPS as MODEL_CLIPS, T0, result
from tests.model.test_scrambling import CODE, PACK, VPS, _packets, recording
from tests.test_playback import START, ezviz, recorded

HLS = (
    '#!/bin/sh\nfor last; do :; done\necho "$@" > "$(dirname "$last")/args"\n'
    'printf x > "$(dirname "$last")/seg0.m4s"\n'
    "printf '#EXTM3U\\n#EXTINF:2.0,\\nseg0.m4s\\n' > \"$last\"\nexec sleep 30\n"
)
BUSY = '#!/bin/sh\necho "method PLAY failed: 453 Not Enough Bandwidth" >&2\nexit 1\n'
# Keeps what arrives on stdin, then writes one segment.
HLS_PIPE = (
    '#!/bin/sh\nfor last; do :; done\ndir="$(dirname "$last")"\necho "$@" > "$dir/args"\n'
    'cat > "$dir/input"\nprintf x > "$dir/seg00000.m4s"\n'
    "printf '#EXTM3U\\n#EXTINF:2.0,\\nseg00000.m4s\\n' > \"$last\"\nexec sleep 30\n"
)


def _script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    script = tmp_path / "ffmpeg"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(playback, "ffmpeg_binary", lambda _hass: str(script))


def _found() -> list[recordings.Recording]:
    return recordings.parse_results(parse(result(MODEL_CLIPS, "OK")), "101")[0]


def test_stretches_join_adjacent_recordings() -> None:
    joined = sessions.stretches(_found())
    assert [(s.start, s.end) for s in joined] == [
        (T0, T0 + timedelta(minutes=20)),
        (T0 + timedelta(minutes=25), T0 + timedelta(minutes=26)),
    ]
    assert joined[1].kinds == {"motion"}


def test_locate() -> None:
    found = _found()
    inside = T0 + timedelta(minutes=5)
    assert sessions.locate(found, inside) == (inside, T0 + timedelta(minutes=20))
    # In a gap playback continues with the next recording.
    assert sessions.locate(found, T0 + timedelta(minutes=21)) == (
        T0 + timedelta(minutes=25),
        T0 + timedelta(minutes=26),
    )
    assert sessions.locate(found, T0 + timedelta(hours=1)) is None


def test_hls_args(tmp_path: Path) -> None:
    original = sessions.hls_args("rtsp://x", sessions.QUALITY_ORIGINAL, "H.265", tmp_path)
    assert original[original.index("-c:v") + 1] == "copy"
    assert "hvc1" in original
    # A connection that sends nothing ends ffmpeg instead of waiting without a message.
    assert original[original.index("-timeout") + 1] == str(sessions.RTSP_TIMEOUT)
    assert original[-1] == str(tmp_path / sessions.PLAYLIST)
    compatible = sessions.hls_args("rtsp://x", sessions.QUALITY_COMPATIBLE, "H.265", tmp_path)
    assert compatible[compatible.index("-c:v") + 1] == "libx264"
    assert "hvc1" not in compatible
    piped = sessions.hls_args(sessions.PIPE, sessions.QUALITY_ORIGINAL, "hevc", tmp_path)
    assert piped[piped.index("-i") - 3 : piped.index("-i") + 2] == [
        "mpeg",
        "-analyzeduration",
        "2000000",
        "-i",
        sessions.PIPE,
    ]
    assert "hvc1" in piped
    assert "-rtsp_transport" not in piped
    assert piped[piped.index("-hls_segment_filename") + 1] == str(tmp_path / "seg%05d.m4s")
    assert "-ss" not in piped


def _keyed(parameters: bytes, key: bytes, other: bytes) -> tuple[bytes, dict[int, int]]:
    """A recorded file with a frame every 2 s and a key frame every 4 s, and where they start."""
    data = b"IMKH" + bytes(36)
    keys: dict[int, int] = {}
    for second in range(0, 16, 2):
        if second % 4 == 0:
            keys[second] = len(data)
            frame = b"\x00\x00\x00\x01" + parameters + b"\x00\x00\x00\x01" + key
        else:
            frame = b"\x00\x00\x00\x01" + other
        data += PACK + _packets(frame + bytes(range(1, 200)), False, 90000 * second)
    return data, keys


async def _fed(stream: ProgramStream, data: bytes):
    for at in range(0, len(data), 333):
        yield stream.feed(data[at : at + 333])
    yield stream.flush()


@pytest.mark.parametrize(
    ("codec", "parameters", "key", "other"),
    [
        ("hevc", VPS, b"\x26\x01", b"\x02\x01"),
        ("h264", b"\x67\x42\x00\x1f", b"\x65\x88", b"\x41\x9a"),
    ],
)
async def test_skipped_starts_at_key_frame(
    codec: str, parameters: bytes, key: bytes, other: bytes
) -> None:
    """The part before the last key frame ahead of the wanted time never reaches ffmpeg."""
    data, keys = _keyed(parameters, key, other)
    stream = ProgramStream(codec=codec)
    chunks = _fed(stream, data)
    first, cut = await sessions.skipped(stream, chunks, 10)
    assert cut == 8
    rest = b"".join([chunk async for chunk in chunks])
    assert first + rest == data[keys[8] :]
    assert stream.key_frames is None

    # Wanted before the first key frame: the file plays from that frame.
    stream = ProgramStream(codec=codec)
    first, cut = await sessions.skipped(stream, _fed(stream, data[: keys[4]]), 1)
    assert cut == 0
    assert first.startswith(data[keys[0] : keys[0] + 50])


async def test_skipped_without_key_frames() -> None:
    data = (
        b"IMKH"
        + bytes(36)
        + PACK
        + _packets(b"\x00\x00\x00\x01\x02\x01" + bytes(range(1, 99)), False, 0)
    )
    stream = ProgramStream(codec="hevc")
    first, cut = await sessions.skipped(stream, _fed(stream, data), 10)
    assert cut == 0
    assert first == recordings.strip_header(data)


def test_covering() -> None:
    found = _found()
    assert sessions.covering(found, T0 + timedelta(minutes=15)).name == "b"
    assert sessions.covering(found, T0 + timedelta(minutes=10)).name == "b"
    # In a gap, the next file.
    assert sessions.covering(found, T0 + timedelta(minutes=21)).name == "c"
    assert sessions.covering(found, T0 + timedelta(hours=1)) is None


def test_played_seconds() -> None:
    playlist = "#EXTM3U\n#EXTINF:4.0,\nseg00000.m4s\n#EXTINF:3.5,\nseg00001.m4s\n#EXTINF:4,\nseg00002.m4s\n"
    assert sessions.played_seconds(playlist, -1) == 0
    assert sessions.played_seconds(playlist, 1) == 7.5
    assert sessions.played_seconds(playlist, 9) == 11.5


@pytest.mark.parametrize(
    ("errors", "scrambled", "reason"),
    [
        ("Could not find codec parameters (dimensions not set)", True, "playback_encrypted"),
        ("Could not find codec parameters (dimensions not set)", False, "playback_unreadable"),
        ("pipe:0: Invalid data found when processing input", False, "playback_unreadable"),
        ("method PLAY failed: 453 Not Enough Bandwidth", False, "playback_busy"),
        ("method DESCRIBE failed: 401 Unauthorized", False, "playback_auth"),
        ("Connection refused", False, "playback_failed"),
    ],
)
def test_reason(errors: str, scrambled: bool, reason: str) -> None:
    assert sessions._reason(errors, scrambled) == reason


async def _setup(hass: HomeAssistant, serve, data: bytes = b""):
    """Without data the device refuses nothing but sends no video: playback uses RTSP."""
    device = FakeDevice("camera_g3")
    recorded(device, data)
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "media_source", {})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return device, entry


async def test_cameras_and_timeline(hass: HomeAssistant, serve, hass_ws_client) -> None:
    _, entry = await _setup(hass, serve)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/cameras"})
    answer = await client.receive_json()
    assert answer["success"]
    [camera] = answer["result"]["cameras"]
    assert camera["entry_id"] == entry.entry_id
    assert camera["channel_id"] == "1"
    assert camera["sources"] == ["card"]
    assert camera["live"]

    period = {"start": (START - timedelta(minutes=5)).isoformat(), "end": START.isoformat()}
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/timeline",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "start": (START - timedelta(minutes=5)).isoformat(),
            "end": (START + timedelta(hours=1)).isoformat(),
        }
    )
    answer = await client.receive_json()
    assert answer["success"]
    timeline = answer["result"]
    assert timeline["source"] == "card"
    assert [r["kind"] for r in timeline["recordings"]] == ["continuous", "motion"]
    assert all(r["token"] for r in timeline["recordings"])
    assert timeline["events"] == []

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/timeline", "entry_id": "nope", "channel_id": "1", "source": "auto"}
        | period
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"

    # The device calendar lists only today.
    today = dt_util.now()
    days = {"type": f"{DOMAIN}/days", "channel_id": "1", "source": "auto", "year": today.year}
    await client.send_json_auto_id(days | {"entry_id": entry.entry_id, "month": today.month})
    answer = await client.receive_json()
    assert answer["success"]
    assert answer["result"] == {"days": [today.day]}
    await client.send_json_auto_id(
        days | {"entry_id": entry.entry_id, "channel_id": "9", "month": 1}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def test_playback_session(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    hass_client_no_auth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, entry = await _setup(hass, serve)
    _script(tmp_path, monkeypatch, HLS)
    client = await hass_ws_client(hass)
    moment = START + timedelta(minutes=5)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": moment.isoformat(),
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    started = answer["result"]
    assert started["start"] == moment.isoformat()
    assert started["end"] == (START + timedelta(minutes=10)).isoformat()
    assert started["quality"] == sessions.QUALITY_ORIGINAL

    session = sessions.manager(hass).sessions[started["session_id"]]
    args = await hass.async_add_executor_job((session.folder / "args").read_text)
    assert "/Streaming/tracks/101/?starttime=" in args
    assert args.startswith("-hide_banner")

    web = await hass_client_no_auth()
    playlist = await web.get(started["url"])
    assert playlist.status == 200
    assert "#EXTINF" in await playlist.text()
    segment = await web.get(started["url"].replace(sessions.PLAYLIST, "seg0.m4s"))
    assert await segment.read() == b"x"
    assert (await web.get(started["url"].replace(sessions.PLAYLIST, "args"))).status == 404
    assert (await web.get(f"/api/{DOMAIN}/session/unknown/{sessions.PLAYLIST}")).status == 404

    # Seeking replaces the session; the camera plays one recording at a time.
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(minutes=30, seconds=30)).isoformat(),
            "quality": sessions.QUALITY_COMPATIBLE,
            "replace": started["session_id"],
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    second = answer["result"]
    assert list(sessions.manager(hass).sessions) == [second["session_id"]]
    assert not await hass.async_add_executor_job(session.folder.exists)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/playback/stop", "session_id": second["session_id"]}
    )
    assert (await client.receive_json())["success"]
    assert not sessions.manager(hass).sessions

    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(hours=1)).isoformat(),
        }
    )
    assert (await client.receive_json())["error"]["code"] == "no_recording"


async def test_playback_session_from_download(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    hass_client_no_auth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The span is downloaded, decrypted with the device's code and fed to ffmpeg."""
    device, entry = await _setup(hass, serve, recording(encrypted=True))
    device.responders[CLOUD_PATH] = ezviz(CODE)
    _script(tmp_path, monkeypatch, HLS_PIPE)
    client = await hass_ws_client(hass)
    moment = START + timedelta(minutes=5)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": moment.isoformat(),
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    started = answer["result"]
    session = sessions.manager(hass).sessions[started["session_id"]]
    args = await hass.async_add_executor_job((session.folder / "args").read_text)
    assert "pipe:0" in args
    assert "hvc1" in args
    fed = await hass.async_add_executor_job((session.folder / "input").read_bytes)
    assert fed == recordings.for_ffmpeg(recording(encrypted=False))
    download = [w for w in device.writes if w[1] == recordings.DOWNLOAD_PATH][-1]
    assert "starttime=" in download[2]
    assert "name=" not in download[2]

    web = await hass_client_no_auth()
    segment = await web.get(started["url"].replace(sessions.PLAYLIST, "seg00000.m4s"))
    assert await segment.read() == b"x"
    assert session.fetched == 0

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/playback/stop", "session_id": started["session_id"]}
    )
    assert (await client.receive_json())["success"]
    assert not sessions.manager(hass).sessions


async def test_playback_session_from_recorded_file(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder that refuses the span but hands out its files plays the file at the time."""
    device, entry = await _setup(hass, serve)
    data = recording(encrypted=False)

    def download(_method: str, body: str) -> web.Response:
        if "name=" in body:
            return web.Response(body=data, content_type="application/binary")
        return web.Response(
            status=500,
            text="<ResponseStatus><statusCode>6</statusCode><statusString>Device Error</statusString></ResponseStatus>",
            content_type="application/xml",
        )

    device.responders[recordings.DOWNLOAD_PATH] = download
    _script(tmp_path, monkeypatch, HLS_PIPE)
    client = await hass_ws_client(hass)

    async def play(moment) -> dict:
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/playback/start",
                "entry_id": entry.entry_id,
                "channel_id": "1",
                "source": "auto",
                "time": moment.isoformat(),
            }
        )
        answer = await client.receive_json()
        assert answer["success"], answer
        return answer["result"]

    started = await play(START + timedelta(minutes=5))
    # The file's only key frame is at its start, so the session starts there.
    assert started["start"] == START.isoformat()
    assert started["end"] == (START + timedelta(minutes=10)).isoformat()
    session = sessions.manager(hass).sessions[started["session_id"]]
    args = await hass.async_add_executor_job((session.folder / "args").read_text)
    assert "-ss" not in args
    fed = await hass.async_add_executor_job((session.folder / "input").read_bytes)
    assert fed == data[:40] + data[40 + len(PACK) :]
    downloads = [w[2] for w in device.writes if w[1] == recordings.DOWNLOAD_PATH]
    # The span, the file's start to compare with, then the file asked from the time on:
    # this device sent it from its start anyway.
    assert len(downloads) == 3
    assert "name=" not in downloads[0]
    assert "name=a" in downloads[1]
    assert f"starttime={recordings.compact(START)}" in downloads[1]
    assert "name=a" in downloads[2]
    assert f"starttime={recordings.compact(START)}" not in downloads[2]

    # The device is known now: the next playback asks for the file straight away.
    started = await play(START + timedelta(minutes=30))
    assert started["end"] == (START + timedelta(minutes=31)).isoformat()
    downloads = [w[2] for w in device.writes if w[1] == recordings.DOWNLOAD_PATH]
    assert len(downloads) == 4
    assert "name=b" in downloads[3]
    await sessions.manager(hass).stop_all()


async def test_playback_session_from_later_in_recorded_file(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder that starts a recorded file at the asked time sends only what is played."""
    monkeypatch.setattr(sessions, "LATER_START_SECONDS", 5)
    device, entry = await _setup(hass, serve)
    data, keys = _keyed(b"\x67\x42\x00\x1f", b"\x65\x88", b"\x41\x9a")
    later = data[:40] + data[keys[8] :]

    def download(_method: str, body: str) -> web.Response:
        if "name=" not in body:
            return web.Response(
                status=400,
                text="<ResponseStatus><statusCode>4</statusCode><statusString>Invalid XML Content</statusString></ResponseStatus>",
                content_type="application/xml",
            )
        whole = f"starttime={recordings.compact(START)}" in body
        return web.Response(body=data if whole else later, content_type="application/binary")

    device.responders[recordings.DOWNLOAD_PATH] = download
    _script(tmp_path, monkeypatch, HLS_PIPE)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(seconds=10)).isoformat(),
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    # The device started at the key frame at 8 s, found from the time stamps.
    assert answer["result"]["start"] == (START + timedelta(seconds=8)).isoformat()
    session = sessions.manager(hass).sessions[answer["result"]["session_id"]]
    fed = await hass.async_add_executor_job((session.folder / "input").read_bytes)
    assert fed == data[:40] + data[keys[8] :]
    await sessions.manager(hass).stop_all()


async def test_playback_beyond_reach_of_recorded_file(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A time further into a file than the device sends in time plays over RTSP, if it can."""
    monkeypatch.setattr(sessions, "SKIP_SECONDS", 0)
    device, entry = await _setup(hass, serve)
    data, _keys = _keyed(b"\x67\x42\x00\x1f", b"\x65\x88", b"\x41\x9a")

    def download(_method: str, body: str) -> web.Response:
        if "name=" not in body:
            return web.Response(
                status=400,
                text="<ResponseStatus><statusCode>4</statusCode><statusString>Invalid XML Content</statusString></ResponseStatus>",
                content_type="application/xml",
            )
        return web.Response(body=data, content_type="application/binary")

    device.responders[recordings.DOWNLOAD_PATH] = download
    _script(tmp_path, monkeypatch, HLS_PIPE)
    client = await hass_ws_client(hass)

    async def play(moment) -> dict:
        await client.send_json_auto_id(
            {
                "type": f"{DOMAIN}/playback/start",
                "entry_id": entry.entry_id,
                "channel_id": "1",
                "source": "auto",
                "time": moment.isoformat(),
            }
        )
        answer = await client.receive_json()
        assert answer["success"], answer
        session = sessions.manager(hass).sessions[answer["result"]["session_id"]]
        return answer["result"], await hass.async_add_executor_job(
            (session.folder / "args").read_text
        )

    # The file is read from its start and ends at its last key frame, 12 s in.
    started, args = await play(START + timedelta(minutes=5))
    assert started["start"] == (START + timedelta(seconds=12)).isoformat()
    assert "pipe:0" in args
    # Further than that: RTSP plays from the time itself.
    started, args = await play(START + timedelta(minutes=6))
    assert started["start"] == (START + timedelta(minutes=6)).isoformat()
    assert "/Streaming/tracks/101/?starttime=" in args

    # Without RTSP playback the file is read up to the time after all.
    _script(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\ncase "$*" in *rtsp://*) echo "Connection refused" >&2; exit 1;; esac\n'
        + HLS_PIPE.removeprefix("#!/bin/sh\n"),
    )
    started, args = await play(START + timedelta(minutes=7))
    assert started["start"] == (START + timedelta(seconds=12)).isoformat()
    assert "pipe:0" in args
    downloads = len([w for w in device.writes if w[1] == recordings.DOWNLOAD_PATH])
    _script(tmp_path, monkeypatch, HLS)
    started, args = await play(START + timedelta(minutes=8))
    assert "pipe:0" in args
    assert len([w for w in device.writes if w[1] == recordings.DOWNLOAD_PATH]) > downloads
    await sessions.manager(hass).stop_all()


async def test_playback_start_replaced_while_starting(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seek while the previous one is still starting drops that one instead of waiting."""
    _, entry = await _setup(hass, serve)
    marker = tmp_path / "first"
    # The first ffmpeg writes nothing; the ones after it play.
    _script(
        tmp_path,
        monkeypatch,
        f'#!/bin/sh\nif [ ! -e "{marker}" ]; then touch "{marker}"; exec sleep 30; fi\n'
        + HLS.removeprefix("#!/bin/sh\n"),
    )
    client = await hass_ws_client(hass)

    def start(minutes: int) -> dict:
        return {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(minutes=minutes)).isoformat(),
        }

    await client.send_json_auto_id(start(2))
    for _ in range(100):
        if await hass.async_add_executor_job(marker.exists):
            break
        await asyncio.sleep(0.05)
    await client.send_json_auto_id(start(4))
    answers = {}
    for _ in range(2):
        answer = await client.receive_json()
        answers[answer["id"]] = answer
    first, second = (answers[i] for i in sorted(answers))
    assert first["error"]["code"] == "playback_replaced"
    assert second["success"], second
    assert list(sessions.manager(hass).sessions) == [second["result"]["session_id"]]
    await sessions.manager(hass).stop_all()


def test_left_out() -> None:
    data, keys = _keyed(b"\x67\x42\x00\x1f", b"\x65\x88", b"\x41\x9a")
    header = data[:40]
    assert sessions.left_out(data, data, 10) is None
    assert sessions.left_out(data, header + data[keys[8] :], 10) == 8
    # Time stamps starting over: taken as started where asked.
    assert sessions.left_out(data, header + b"\x00\x00\x01\xba" + bytes(20), 10) == 10


def test_starting_at() -> None:
    uri = "rtsp://x/Streaming/tracks/101/?starttime=20260101T000000Z&endtime=20260101T010000Z&name=a&size=9"
    moved = recordings.starting_at(uri, T0 + timedelta(minutes=5), timedelta(hours=1))
    assert moved == uri.replace(
        "starttime=20260101T000000Z",
        f"starttime={recordings.compact(T0 + timedelta(minutes=5), timedelta(hours=1))}",
    )
    assert recordings.starting_at("rtsp://x/Streaming/tracks/101/?name=a", T0) is None


async def test_playback_busy(
    hass: HomeAssistant, serve, hass_ws_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)
    _script(tmp_path, monkeypatch, BUSY)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "card",
            "time": (START + timedelta(minutes=5)).isoformat(),
        }
    )
    assert (await client.receive_json())["error"]["code"] == "playback_busy"
    assert not sessions.manager(hass).sessions
