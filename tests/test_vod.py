"""Recorded files in the media browser as seekable video on demand (HLS)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import re
import struct

from homeassistant.components import media_source
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hikvision_bridge.const import OPT_PLAYBACK, PLAYBACK_SEEKABLE
from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.runtime import sessions, vod
from custom_components.hikvision_bridge.views import encode_token
from tests.model.test_recordings import program_stream
from tests.test_panel import _script
from tests.test_playback import START, _channel_media, _setup

URI = (
    f"rtsp://192.0.2.10:554/Streaming/tracks/101/?starttime={recordings.compact(START)}"
    f"&endtime={recordings.compact(START + timedelta(minutes=10))}&name=a&size=1000"
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _pieces(folder: Path, count: int) -> list[bytes]:
    """What ffmpeg writes: an init section and pieces of one second; their fragments."""
    folder.mkdir()
    (folder / vod.INIT).write_bytes(b"init")
    playlist = "#EXTM3U\n"
    fragments = []
    for number in range(count):
        fragment = _box(b"moof", b"%d" % number) + _box(b"mdat", b"video")
        (folder / sessions.SEGMENT.format(number)).write_bytes(
            _box(b"styp", b"msdh") + _box(b"sidx", b"index") + fragment
        )
        fragments.append(fragment)
        playlist += f"#EXTINF:1.0,\n{sessions.SEGMENT.format(number)}\n"
    (folder / sessions.PLAYLIST).write_text(playlist)
    return fragments


async def test_seekable_playback(
    hass: HomeAssistant,
    serve,
    hass_client,
    hass_client_no_auth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, entry = await _setup(hass, serve, program_stream(), **{OPT_PLAYBACK: PLAYBACK_SEEKABLE})
    fragments = _pieces(tmp_path / "pieces", 8)
    _script(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\nfor last; do :; done\ndir="$(dirname "$last")"\necho "$@" > "$dir/args"\n'
        f'cp {tmp_path / "pieces"}/* "$dir/"\nexec sleep 30\n',
    )
    played = await media_source.async_resolve_media(
        hass, f"{_channel_media(entry)}/card/clip/{encode_token(URI)}", None
    )
    assert played.mime_type == "application/x-mpegURL"
    path = played.url.split("?", 1)[0]
    assert "/seekable/" in path

    # The playlist needs the login (Home Assistant signs its URL); its parts carry a token.
    web = await hass_client_no_auth()
    assert (await web.get(path)).status == 401
    playlist = await (await (await hass_client()).get(path)).text()
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist
    assert playlist.rstrip().endswith("#EXT-X-ENDLIST")
    assert playlist.count("#EXTINF:4.000,") == 150
    init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)[1]
    parts = [line for line in playlist.splitlines() if line.endswith(".m4s")]
    assert len(parts) == 150

    assert await (await web.get(init)).read() == b"init"
    session = next(iter(sessions.manager(hass).sessions.values()))
    args = (await hass.async_add_executor_job((session.folder / "args").read_text)).split()
    # Time stamps from the start of the file, in the fragments, cut at every key frame.
    assert args[args.index("-output_ts_offset") + 1] == "0.000"
    assert "movflags=+frag_discont" in args
    assert args[args.index("-hls_time") + 1] == "1"

    # A part is the fragments of the pieces that start in it.
    assert await (await web.get(parts[0])).read() == b"".join(fragments[:4])
    assert await (await web.get(parts[1])).read() == b"".join(fragments[4:])
    # A part the session does not reach in time is refused, not sent short.
    monkeypatch.setattr(vod, "WAIT_SECONDS", 0.3)
    assert (await web.get(parts[2])).status == 503
    # Only plain part numbers are parts.
    for name in ("².m4s", "-1.m4s", "1.mp4", "9" * 30 + ".m4s"):
        assert (await web.get(parts[0].replace("/0.m4s", f"/{name}"))).status == 404

    # Far ahead the session starts again there.
    assert await (await web.get(parts[100])).read() == b"".join(fragments[:4])
    (session,) = sessions.manager(hass).sessions.values()
    assert session.offset == 400
    args = (await hass.async_add_executor_job((session.folder / "args").read_text)).split()
    assert args[args.index("-output_ts_offset") + 1] == "400.000"

    assert (await web.get(parts[0].replace("/0.m4s", "/150.m4s"))).status == 404
    assert (await web.get(init.replace(init.split("/")[-2], "unknown"))).status == 404

    # A deactivated owner no longer gets the video.
    (played,) = hass.data[vod.DATA_KEY].values()
    owner = await hass.auth.async_create_user("Viewer")
    played.owner = owner.id
    await hass.auth.async_deactivate_user(owner)
    assert (await web.get(parts[1])).status == 404
    assert not hass.data[vod.DATA_KEY]

    # Unloading the entry forgets its files.
    assert (await (await hass_client()).get(path)).status == 200
    assert hass.data[vod.DATA_KEY]
    await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.data[vod.DATA_KEY]


def test_playlist_ends_with_the_rest() -> None:
    recording = recordings.Recording("101", START, START + timedelta(seconds=9), "", URI)
    played = vod.Vod("token", ("entry", "1", "card"), "owner", recording)
    playlist = played.playlist()
    assert played.parts == 3
    assert playlist.count("#EXTINF:4.000,") == 2
    assert "#EXTINF:1.000,\n/api/hikvision_bridge/vod/token/2.m4s" in playlist


def _vod(hass: HomeAssistant, token: str, owner: str, used: float) -> vod.Vod:
    recording = recordings.Recording("101", START, START + timedelta(minutes=1), "", token)
    played = vod.Vod(token, ("entry", "1", "card"), owner, recording, used=used)
    hass.data.setdefault(vod.DATA_KEY, {})[token] = played
    return played


def _source():
    async def wall_offset(_link):
        return timedelta(0)

    return type("Source", (), {"link": None, "track": "101"})(), wall_offset


async def test_full_room_is_made_from_idle_files(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wall_offset = _source()
    monkeypatch.setattr(recordings, "wall_offset", wall_offset)
    now = vod.time.monotonic()
    idle = now - sessions.IDLE_SECONDS - 1
    for number in range(vod.MAX_VODS):
        _vod(hass, f"t{number}", "other", now)
    # Every file is played at the moment: no room, nobody loses theirs.
    with pytest.raises(vod.VodsFull):
        await vod.create(hass, ("entry", "1", "card"), source, URI, "me")
    assert len(hass.data[vod.DATA_KEY]) == vod.MAX_VODS
    # Idle files make room, the owner's own first, then the longest idle.
    hass.data[vod.DATA_KEY]["t0"].used = idle - 100
    hass.data[vod.DATA_KEY]["t1"].used = idle
    hass.data[vod.DATA_KEY]["t1"].owner = "me"
    played = await vod.create(hass, ("entry", "1", "card"), source, URI, "me")
    assert played is not None
    assert "t1" not in hass.data[vod.DATA_KEY]
    assert "t0" in hass.data[vod.DATA_KEY]
    # The same file again is the same video on demand.
    assert await vod.create(hass, ("entry", "1", "card"), source, URI, "me") is played


async def test_lifetime_counts_from_the_last_use(hass: HomeAssistant) -> None:
    now = vod.time.monotonic()
    played = _vod(hass, "paused", "me", now - sessions.MAX_LIFETIME + 10)
    # Paused for nearly the lifetime: still there, and the use starts it again.
    assert vod.get(hass, "paused") is played
    assert played.used >= now
    played.used = now - sessions.MAX_LIFETIME - 1
    assert vod.get(hass, "paused") is None
    assert "paused" not in hass.data[vod.DATA_KEY]


async def test_drop_entry_keeps_other_entries(hass: HomeAssistant) -> None:
    now = vod.time.monotonic()
    _vod(hass, "a", "me", now)
    other = _vod(hass, "b", "me", now)
    other.key = ("other", "1", "card")
    vod.drop_entry(hass, "entry")
    assert list(hass.data[vod.DATA_KEY]) == ["b"]


def test_offset_is_never_negative() -> None:
    session = sessions.Session("id", ("e", "1", "card"), START, START, "original", Path("x"))
    assert session.offset is None
    session.origin = START + timedelta(seconds=2)
    assert session.offset == 0.0
    session.origin = START - timedelta(seconds=8)
    assert session.offset == 8.0
