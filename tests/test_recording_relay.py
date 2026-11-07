"""Recorded RTSP fallback uses the same local H.264 decryption as live video."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import stat
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.hikvision_bridge.runtime import playback, sessions
from custom_components.hikvision_bridge.runtime.live import LiveRelay
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink


@pytest.mark.parametrize(
    ("code", "codec", "decrypted"),
    [
        ("ABCDEF", "h264", True),
        (None, "h264", False),
        ("ABCDEF", "H.265", False),
        ("ABCDEF", None, False),
    ],
)
async def test_recording_rtsp_fallback_uses_code_and_closes_relay(
    hass: HomeAssistant,
    socket_enabled,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code,
    codec,
    decrypted,
) -> None:
    script = tmp_path / "ffmpeg"
    script.write_text(
        '#!/bin/sh\nfor last; do :; done\ndir="$(dirname "$last")"\n'
        'printf "%s\\n" "$@" > "$dir/args"\nprintf x > "$dir/seg00000.m4s"\n'
        'printf "#EXTM3U\\n#EXTINF:2.0,\\nseg00000.m4s\\n" > "$last"\nexec sleep 30\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(playback, "ffmpeg_binary", lambda _hass: str(script))
    link = IsapiLink(async_get_clientsession(hass), Endpoint("127.0.0.1", 80), "user", "pass")
    source = playback.PlaybackSource(
        "recorder", link, "101", {"username": "user", "password": "pass"}, 554, code
    )
    manager = sessions.SessionManager(hass)
    start = datetime.now(UTC)
    uri = sessions.playback_uri(source, start, start + timedelta(minutes=5), timedelta())
    session = await manager._from_rtsp(
        source,
        ("entry", "1", "recorder"),
        start,
        start + timedelta(minutes=5),
        uri,
        "original",
        codec,
        tmp_path,
    )
    try:
        args = (tmp_path / "args").read_text().splitlines()
        parsed = urlsplit(args[args.index("-i") + 1])
        assert parsed.hostname == "127.0.0.1"
        if not decrypted:
            assert parsed.username == "user"
            assert parsed.path == "/Streaming/tracks/101/"
            assert session.relay is None
            return
        assert parsed.username is None
        assert parsed.password is None
        assert "/Streaming/tracks/" not in parsed.path
        assert session.relay.upstream.endswith(uri.split("rtsp://device", 1)[1])
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        await manager._end(session)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        assert session.relay.server is None
    finally:
        await manager._end(session)


async def test_failed_recording_spawn_closes_relay(hass, socket_enabled, tmp_path, monkeypatch):
    relays = []

    def capture(*args, **kwargs):
        relay = LiveRelay(*args, **kwargs)
        relays.append(relay)
        return relay

    async def fail(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(sessions, "LiveRelay", capture)
    monkeypatch.setattr(sessions.SessionManager, "_spawn", fail)
    link = IsapiLink(async_get_clientsession(hass), Endpoint("127.0.0.1", 80), "user", "pass")
    source = playback.PlaybackSource(
        "recorder", link, "101", {"username": "user", "password": "pass"}, 554, "ABCDEF"
    )
    start = datetime.now(UTC)
    uri = sessions.playback_uri(source, start, start + timedelta(minutes=5), timedelta())
    with pytest.raises(FileNotFoundError):
        await sessions.SessionManager(hass)._from_rtsp(
            source,
            ("entry", "1", "recorder"),
            start,
            start + timedelta(minutes=5),
            uri,
            "original",
            "h264",
            tmp_path,
        )
    assert len(relays) == 1
    assert relays[0].server is None
    assert not relays[0].tasks
