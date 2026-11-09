"""Recorded files in the media browser as seekable video on demand (HLS)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import re
import struct

from homeassistant.components import media_source
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hikvision_bridge.const import DOMAIN
from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.runtime import sessions, vod
from custom_components.hikvision_bridge.views import encode_token
from tests.model.test_recordings import program_stream
from tests.test_panel import _script
from tests.test_playback import START, _setup

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
    _, entry = await _setup(hass, serve, program_stream())
    fragments = _pieces(tmp_path / "pieces", 8)
    _script(
        tmp_path,
        monkeypatch,
        '#!/bin/sh\nfor last; do :; done\ndir="$(dirname "$last")"\necho "$@" > "$dir/args"\n'
        f'cp {tmp_path / "pieces"}/* "$dir/"\nexec sleep 30\n',
    )
    # Seekable video on demand is the default.
    played = await media_source.async_resolve_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/1/card/clip/{encode_token(URI)}", None
    )
    assert played.mime_type == "application/vnd.apple.mpegurl"
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


def test_playlist_ends_with_the_rest() -> None:
    recording = recordings.Recording("101", START, START + timedelta(seconds=9), "", URI)
    played = vod.Vod("token", ("entry", "1", "card"), "owner", recording, START)
    playlist = played.playlist()
    assert played.parts == 3
    assert playlist.count("#EXTINF:4.000,") == 2
    assert "#EXTINF:1.000,\n/api/hikvision_bridge/vod/token/2.m4s" in playlist


async def test_file_listed_from_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file running into the day searched starts earlier than it is listed."""
    midnight = dt_util.start_of_local_day(START)
    real = midnight - timedelta(days=1, hours=3)
    asked = []

    async def search(_link, _track, start, end):
        asked.append(start)
        return [recordings.Recording("101", max(start, real), end, "", URI, "a")]

    monkeypatch.setattr(recordings, "search", search)
    source = type("Source", (), {"link": None, "track": "101"})()
    listed = recordings.Recording("101", midnight, START, "", URI, "a")
    assert await vod._file_start(source, listed) == real
    assert asked == [midnight - timedelta(days=1), midnight - timedelta(days=2)]
    # Files listed from any other time start there.
    later = recordings.Recording("101", START, START, "", URI, "a")
    assert await vod._file_start(source, later) == START
    assert len(asked) == 2
