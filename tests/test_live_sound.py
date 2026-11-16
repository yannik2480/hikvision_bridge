"""Live picture with its own sound: the stream address and the ffmpeg arguments for it."""

from __future__ import annotations

from pathlib import Path

from custom_components.hikvision_bridge.runtime import sessions


def test_live_rtsp_url_quotes_the_login() -> None:
    url = sessions.live_rtsp_url("192.0.2.10", 554, "201", "ad min", "p@ss/word")
    assert url == "rtsp://ad%20min:p%40ss%2Fword@192.0.2.10:554/Streaming/Channels/201"


def test_live_hls_keeps_a_window_and_converts_sound(tmp_path: Path) -> None:
    args = sessions.hls_args(
        "rtsp://x", sessions.QUALITY_ORIGINAL, "H.265", tmp_path, True, live=True
    )
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"
    assert "-allowed_media_types" not in args
    assert args[args.index("-hls_list_size") + 1] == str(sessions.LIVE_WINDOW_SEGMENTS)
    assert "-hls_playlist_type" not in args
    flags = args[args.index("-hls_flags") + 1]
    assert "delete_segments" in flags


def test_recording_hls_is_unchanged(tmp_path: Path) -> None:
    args = sessions.hls_args("rtsp://x", sessions.QUALITY_ORIGINAL, "H.265", tmp_path, True)
    assert args[args.index("-hls_list_size") + 1] == "0"
    assert args[args.index("-hls_playlist_type") + 1] == "event"
    assert args[args.index("-hls_flags") + 1] == "independent_segments"
