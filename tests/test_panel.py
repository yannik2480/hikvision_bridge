"""Panel and card backend: camera list, timeline, playback sessions and their files."""

from __future__ import annotations

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
from custom_components.hikvision_bridge.runtime import playback, sessions
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice
from tests.model.test_recordings import CLIPS as MODEL_CLIPS, T0, result
from tests.model.test_scrambling import CODE, recording
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
    skipped = sessions.hls_args(sessions.PIPE, sessions.QUALITY_ORIGINAL, "hevc", tmp_path, 90)
    # Left out after the input, so that ffmpeg reads up to there instead of seeking a pipe.
    assert skipped[skipped.index("-i") + 2 : skipped.index("-i") + 4] == ["-ss", "90.000"]


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
    ("errors", "reason"),
    [
        ("Could not find codec parameters (dimensions not set)", "playback_encrypted"),
        ("method PLAY failed: 453 Not Enough Bandwidth", "playback_busy"),
        ("method DESCRIBE failed: 401 Unauthorized", "playback_auth"),
        ("Connection refused", "playback_failed"),
    ],
)
def test_reason(errors: str, reason: str) -> None:
    assert sessions._reason(errors) == reason


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
    assert fed == recordings.strip_header(recording(encrypted=False))
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

    moment = START + timedelta(minutes=5)
    started = await play(moment)
    assert started["start"] == moment.isoformat()
    assert started["end"] == (START + timedelta(minutes=10)).isoformat()
    session = sessions.manager(hass).sessions[started["session_id"]]
    args = await hass.async_add_executor_job((session.folder / "args").read_text)
    assert "pipe:0 -ss 300.000 " in args
    fed = await hass.async_add_executor_job((session.folder / "input").read_bytes)
    assert fed == recordings.strip_header(data)
    downloads = [w[2] for w in device.writes if w[1] == recordings.DOWNLOAD_PATH]
    assert len(downloads) == 2
    assert "name=" not in downloads[0]
    assert "name=a" in downloads[1]

    # The device is known now: the next playback asks for the file straight away.
    started = await play(START + timedelta(minutes=30))
    assert started["end"] == (START + timedelta(minutes=31)).isoformat()
    downloads = [w[2] for w in device.writes if w[1] == recordings.DOWNLOAD_PATH]
    assert len(downloads) == 3
    assert "name=b" in downloads[2]
    await sessions.manager(hass).stop_all()


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
