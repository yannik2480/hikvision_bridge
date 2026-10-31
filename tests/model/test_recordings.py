"""Recording search, day overview, download inspection and conversion arguments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re

import aiohttp
from aiohttp import web
import pytest

from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.runtime import playback
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.fakedevice import FakeDevice

T0 = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def match(start: datetime, minutes: int, kind: str, name: str, track: str = "101") -> str:
    end = start + timedelta(minutes=minutes)
    uri = (
        f"rtsp://192.0.2.10:554/Streaming/tracks/{track}/?starttime={recordings.compact(start)}"
        f"&amp;endtime={recordings.compact(end)}&amp;name={name}&amp;size=1000"
    )
    return (
        f"<searchMatchItem><trackID>{track}</trackID><timeSpan>"
        f"<startTime>{start:%Y-%m-%dT%H:%M:%SZ}</startTime>"
        f"<endTime>{end:%Y-%m-%dT%H:%M:%SZ}</endTime></timeSpan>"
        "<mediaSegmentDescriptor><contentType>video</contentType><codecType>H.264-BP</codecType>"
        f"<playbackURI>{uri}</playbackURI><lockStatus>unlock</lockStatus><name>{name}</name>"
        "</mediaSegmentDescriptor><metadataMatches>"
        f"<metadataDescriptor>recordType.meta.hikvision.com/{kind}</metadataDescriptor>"
        "</metadataMatches></searchMatchItem>"
    )


def result(items: list[str], status: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<CMSearchResult version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        f"<responseStatus>true</responseStatus><responseStatusStrg>{status}</responseStatusStrg>"
        f"<numOfMatches>{len(items)}</numOfMatches><matchList>{''.join(items)}</matchList>"
        "</CMSearchResult>"
    )


CLIPS = [
    match(T0, 10, "timing", "a"),
    match(T0 + timedelta(minutes=10), 10, "timing", "b"),
    match(T0 + timedelta(minutes=25), 1, "motion", "c"),
]


def searcher(items: list[str], page: int | None) -> callable:
    """Search answers paging by position, or everything at once (page None) with MORE."""

    def respond(_method: str, body: str) -> web.Response:
        if page is None:
            return web.Response(text=result(items, "MORE"), content_type="application/xml")
        position = int(re.search(r"<searchResultPostion>(\d+)", body)[1])
        chunk = items[position : position + page]
        more = position + page < len(items)
        return web.Response(
            text=result(chunk, "MORE" if more else "OK"), content_type="application/xml"
        )

    return respond


def test_parse_results() -> None:
    found, more = recordings.parse_results(parse(result(CLIPS, "MORE")), "101")
    assert more
    assert [r.kind for r in found] == ["continuous", "continuous", "motion"]
    first = found[0]
    assert first.start == T0
    assert first.duration == timedelta(minutes=10)
    assert first.name == "a"
    assert first.size == 1000
    assert "&endtime=" in first.playback_uri


def test_kinds_and_times() -> None:
    assert recordings.kind_of("recordType.meta.std-cgi.com/VMD") == "motion"
    assert recordings.kind_of("recordType.meta.hikvision.com/AlarmInput") == "alarm"
    assert recordings.kind_of("recordType.meta.hikvision.com/lineDetection") == "event"
    assert recordings.kind_of("recordType.meta.hikvision.com/unheard") == "other"
    assert recordings.kind_of(None) == "other"
    assert recordings.parse_time("20260301T080000Z") == T0
    assert recordings.parse_time("2026-03-01T09:00:00+01:00") == T0
    assert recordings.parse_time("2026-03-01T08:00:00") == T0
    assert recordings.parse_time("garbage") is None
    assert recordings.track_of("2") == "201"


def test_parse_days() -> None:
    document = parse(
        "<trackDailyDistribution><dayList>"
        "<day><id>1</id><dayOfMonth>1</dayOfMonth><record>true</record></day>"
        "<day><id>2</id><dayOfMonth>2</dayOfMonth><record>false</record></day>"
        "<day><id>3</id><dayOfMonth>3</dayOfMonth><record>true</record>"
        "<recordType>time</recordType></day>"
        "</dayList></trackDailyDistribution>"
    )
    assert recordings.parse_days(document) == [1, 3]
    # A bare status answer is no calendar.
    status = parse("<ResponseStatus><statusCode>1</statusCode></ResponseStatus>")
    assert recordings.parse_days(status) is None


@pytest.mark.parametrize("page", [1, 2, None])
async def test_search_pages(serve, page: int | None) -> None:
    device = FakeDevice("camera_g3")
    device.responders[recordings.SEARCH_PATH] = searcher(CLIPS, page)
    server = await serve(device)
    async with aiohttp.ClientSession() as session:
        link = IsapiLink(
            session, Endpoint(server.host, server.port), device.username, device.password
        )
        found = await recordings.search(link, "101", T0, T0 + timedelta(hours=1))
        # Only recordings overlapping the period.
        later = await recordings.search(
            link, "101", T0 + timedelta(minutes=15), T0 + timedelta(minutes=20)
        )
    assert [r.name for r in found] == ["a", "b", "c"]
    assert [r.name for r in later] == ["b"]
    searches = [w for w in device.writes if w[1] == recordings.SEARCH_PATH]
    assert "<trackID>101</trackID>" in searches[0][2]
    assert "<startTime>2026-03-01T08:00:00Z</startTime>" in searches[0][2]


def test_wall_offset_of() -> None:
    now = datetime(2026, 10, 22, 14, 27, 46, tzinfo=UTC)
    # Summer time on the wall clock although the stated zone says +01:00.
    assert recordings.wall_offset_of("2026-10-22T16:27:44+01:00", now) == timedelta(hours=2)
    assert recordings.wall_offset_of("2026-10-22T14:27:40Z", now) == timedelta(0)
    assert recordings.wall_offset_of("2026-10-22T19:57:50", now) == timedelta(hours=5, minutes=30)
    assert recordings.wall_offset_of(None, now) == timedelta(0)
    assert recordings.wall_offset_of("garbage", now) == timedelta(0)


def test_times_on_the_wall_clock() -> None:
    wall = timedelta(hours=2)
    assert recordings.parse_time("20260301T100000Z", wall) == T0
    assert recordings.parse_time("2026-03-01T10:00:00Z", wall) == T0
    # A stated offset is taken as it is.
    assert recordings.parse_time("2026-03-01T09:00:00+01:00", wall) == T0
    assert recordings.compact(T0, wall) == "20260301T100000Z"
    found, _ = recordings.parse_results(parse(result(CLIPS, "OK")), "101", wall)
    assert found[0].start == T0 - wall


async def test_search_on_the_wall_clock(serve) -> None:
    """The device takes and gives times on its wall clock, two hours ahead of UTC here."""
    device = FakeDevice("camera_g3")
    device.responders[recordings.SEARCH_PATH] = searcher(CLIPS, None)
    wall_now = datetime.now(UTC) + timedelta(hours=2)
    device.responders[recordings.TIME_PATH] = lambda _m, _b: web.Response(
        text=f"<Time><localTime>{wall_now:%Y-%m-%dT%H:%M:%S}+01:00</localTime></Time>",
        content_type="application/xml",
    )
    server = await serve(device)
    async with aiohttp.ClientSession() as session:
        link = IsapiLink(
            session, Endpoint(server.host, server.port), device.username, device.password
        )
        start = T0 - timedelta(hours=2)
        found = await recordings.search(link, "101", start, start + timedelta(hours=1))
    searches = [w for w in device.writes if w[1] == recordings.SEARCH_PATH]
    assert "<startTime>2026-03-01T08:00:00Z</startTime>" in searches[0][2]
    assert [r.start for r in found][:1] == [T0 - timedelta(hours=2)]


def program_stream(*, scrambled: bool = False, codec: str = "hevc") -> bytes:
    """IMKH header, a pack header and video packets as the devices write them."""
    flags = 0xAC if scrambled else 0x8C
    nal = b"\x00\x00\x00\x01\x40\x01" if codec == "hevc" else b"\x00\x00\x00\x01\x67\x64"
    packet = b"\x00\x00\x01\xe0\x00\x20" + bytes([flags, 0x80, 0x05]) + b"\x21\x00\x01\x00\x01"
    header = recordings.HEADER_MAGIC + b"\x01\x02" + bytes(34)
    return header + b"\x00\x00\x01\xba" + bytes(10) + (packet + nal + bytes(16)) * 3


def test_inspect_head() -> None:
    clean = recordings.inspect_head(program_stream())
    assert clean == recordings.MediaHead(header=True, codec="hevc", scrambled=False)
    assert recordings.inspect_head(program_stream(codec="h264")).codec == "h264"
    assert recordings.inspect_head(program_stream(scrambled=True)).scrambled
    assert recordings.strip_header(program_stream())[:4] == b"\x00\x00\x01\xba"
    # Downloads by time put 24 more bytes before the header.
    wrapped = b"\x00\x00\x00\x40" + bytes(16) + b"\x00\x00\x00\x28" + program_stream()
    assert recordings.inspect_head(wrapped).header
    assert recordings.strip_header(wrapped) == recordings.strip_header(program_stream())
    assert recordings.strip_header(b"\x00\x00\x01\xba") == b"\x00\x00\x01\xba"
    # ffmpeg gets the header first: it tells the audio format.
    assert recordings.file_header(wrapped) == program_stream()[:40]
    assert recordings.for_ffmpeg(wrapped) == program_stream()
    assert recordings.for_ffmpeg(program_stream()) == program_stream()
    assert recordings.file_header(b"\x00\x00\x01\xba") == b""
    assert recordings.for_ffmpeg(b"\x00\x00\x01\xba") == b"\x00\x00\x01\xba"


def test_ffmpeg_args() -> None:
    copy = playback.ffmpeg_args("mp4", "hevc")
    assert copy[copy.index("-c:v") + 1] == "copy"
    assert "hvc1" in copy
    assert "frag_keyframe+empty_moov+default_base_moof" in copy
    assert copy[-1] == "pipe:1"
    assert "hvc1" not in playback.ffmpeg_args("mp4", "h264")
    saved = playback.ffmpeg_args("mp4", "hevc", target="/media/x.mp4", offset=5, duration=10)
    assert "+faststart" in saved
    assert saved[saved.index("-ss") + 1] == "5.000"
    assert saved[saved.index("-t") + 1] == "10.000"
    assert saved[-1] == "/media/x.mp4"
    h264 = playback.ffmpeg_args("h264", "hevc")
    assert h264[h264.index("-c:v") + 1] == "libx264"
    assert "hvc1" not in h264
    ts = playback.ffmpeg_args("ts", "hevc")
    assert ts[ts.index("-f", ts.index("-c:v")) + 1] == "mpegts"


def test_trim() -> None:
    found, _ = recordings.parse_results(parse(result(CLIPS, "OK")), "101")
    first = found[0]
    assert playback.trim(first, T0 + timedelta(minutes=2), T0 + timedelta(minutes=5)) == (
        120.0,
        180.0,
    )
    # A period covering the whole file needs no cut.
    assert playback.trim(first, T0 - timedelta(minutes=1), T0 + timedelta(hours=1)) == (
        None,
        None,
    )
