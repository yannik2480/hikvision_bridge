"""Recording search, day overview and the shape of downloaded recordings.

Search results carry an RTSP ``playbackURI`` that names one recorded file (start, end,
file name, size). The same URI is handed back to ``/ISAPI/ContentMgmt/download``, which
answers with that whole file. All times in searches and playback URIs are on the device's
wall clock, although marked as UTC; they are converted with the offset of that clock.
Devices write their recordings as MPEG program stream, usually behind a 40 byte ``IMKH``
header; the codec field of search results is not reliable and is therefore not used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
import time
from urllib.parse import parse_qsl, urlsplit
import uuid
from weakref import WeakKeyDictionary
from xml.sax.saxutils import escape

from aiohttp import ClientResponse

from ...wire.faults import BridgeFault, DeviceRejected, Unsupported
from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode

SEARCH_PATH = "/ISAPI/ContentMgmt/search"
TIME_PATH = "/ISAPI/System/time"
# The clock offset is read again after this long (daylight saving time changes it).
OFFSET_TTL = 3600
_NO_OFFSET = timedelta(0)
DOWNLOAD_PATH = "/ISAPI/ContentMgmt/download"
DAYS_PATH = "/ISAPI/ContentMgmt/record/tracks/{track}/dailyDistribution"
PAGE_SIZE = 50
# A day of ten-minute files on one track stays far below this; it only stops runaway paging.
MAX_RESULTS = 2000
HEADER_MAGIC = b"IMKH"
PACK_START = b"\x00\x00\x01\xba"

KIND_CONTINUOUS = "continuous"
KIND_MOTION = "motion"
KIND_ALARM = "alarm"
KIND_EVENT = "event"
KIND_MANUAL = "manual"
KIND_OTHER = "other"
KINDS = (KIND_CONTINUOUS, KIND_MOTION, KIND_ALARM, KIND_EVENT, KIND_MANUAL, KIND_OTHER)

# Last segment of the recordType metadata of a search result.
_KIND_OF = {
    "timing": KIND_CONTINUOUS,
    "time": KIND_CONTINUOUS,
    "cmr": KIND_CONTINUOUS,
    "continuous": KIND_CONTINUOUS,
    "motion": KIND_MOTION,
    "vmd": KIND_MOTION,
    "alarm": KIND_ALARM,
    "alarminput": KIND_ALARM,
    "io": KIND_ALARM,
    "manual": KIND_MANUAL,
    "command": KIND_MANUAL,
    "smart": KIND_EVENT,
    "vca": KIND_EVENT,
    "allevent": KIND_EVENT,
    "linedetection": KIND_EVENT,
    "fielddetection": KIND_EVENT,
    "regionentrance": KIND_EVENT,
    "regionexiting": KIND_EVENT,
    "facedetection": KIND_EVENT,
    "humanrecognition": KIND_EVENT,
    "vehicledetection": KIND_EVENT,
    "eventtrigger": KIND_EVENT,
}


@dataclass(frozen=True, slots=True)
class Recording:
    """One recorded file on a track."""

    track: str
    start: datetime
    end: datetime
    kind: str
    playback_uri: str
    name: str | None = None
    size: int | None = None
    locked: bool = False

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def track_of(channel_id: str) -> str:
    """Main stream track of a channel (channel 2 records on track 201)."""
    return f"{int(channel_id) * 100 + 1}" if channel_id.isdigit() else "101"


def wall_offset_of(local_time: str | None, now: datetime) -> timedelta:
    """How far the device's wall clock is ahead of UTC, in quarter hours.

    Taken from the clock reading of ``/ISAPI/System/time``; the zone it states is not
    reliable (it leaves out daylight saving time).
    """
    if not local_time:
        return timedelta(0)
    try:
        wall = datetime.strptime(local_time.strip()[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return timedelta(0)
    quarters = round((wall.replace(tzinfo=UTC) - now).total_seconds() / 900)
    return timedelta(minutes=15 * quarters)


_offsets: WeakKeyDictionary[IsapiLink, tuple[float, timedelta]] = WeakKeyDictionary()


async def wall_offset(link: IsapiLink) -> timedelta:
    """The device's wall clock ahead of UTC, read again every OFFSET_TTL seconds."""
    cached = _offsets.get(link)
    if cached is not None and time.monotonic() - cached[0] < OFFSET_TTL:
        return cached[1]
    try:
        reply = await link.call("GET", TIME_PATH)
        offset = wall_offset_of(reply.xml().text("localTime"), datetime.now(UTC))
    except BridgeFault:
        offset = cached[1] if cached is not None else timedelta(0)
    _offsets[link] = (time.monotonic(), offset)
    return offset


def parse_time(text: str | None, wall: timedelta = _NO_OFFSET) -> datetime | None:
    """Device time stamp as UTC.

    Devices write their own wall clock and mark it as UTC ("Z") or not at all; ``wall`` is
    how far that clock is ahead of UTC. Stamps with a stated offset are taken as they are.
    """
    if not text:
        return None
    text = text.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC) - wall
        except ValueError:
            continue
    try:
        moment = datetime.fromisoformat(text.removesuffix("Z"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)
    return moment.replace(tzinfo=UTC) - wall


def _iso(moment: datetime, wall: timedelta) -> str:
    return f"{moment.astimezone(UTC) + wall:%Y-%m-%dT%H:%M:%SZ}"


def compact(moment: datetime, wall: timedelta = _NO_OFFSET) -> str:
    """Time stamp in the form of playback URIs, on the device's wall clock."""
    return f"{moment.astimezone(UTC) + wall:%Y%m%dT%H%M%SZ}"


def search_body(
    track: str, start: datetime, end: datetime, position: int = 0, wall: timedelta = _NO_OFFSET
) -> str:
    """Search for all recordings of a track; the kind is read from the results."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<CMSearchDescription version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        f"<searchID>{{{uuid.uuid4()}}}</searchID>"
        f"<trackList><trackID>{escape(track)}</trackID></trackList>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{_iso(start, wall)}</startTime><endTime>{_iso(end, wall)}</endTime>"
        "</timeSpan></timeSpanList>"
        f"<maxResults>{PAGE_SIZE}</maxResults>"
        f"<searchResultPostion>{position}</searchResultPostion>"
        "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>"
        "</metadataList></CMSearchDescription>"
    )


def kind_of(descriptor: str | None) -> str:
    if not descriptor:
        return KIND_OTHER
    last = descriptor.rstrip("/").rsplit("/", 1)[-1].lower()
    return _KIND_OF.get(last, KIND_OTHER)


def parse_results(
    document: XmlNode, fallback_track: str, wall: timedelta = _NO_OFFSET
) -> tuple[list[Recording], bool]:
    """Recordings of one result page and whether the device has more."""
    status = (document.text("responseStatusStrg") or "").upper()
    found: list[Recording] = []
    for item in document.all("matchList/searchMatchItem"):
        start = parse_time(item.text("timeSpan/startTime"), wall)
        end = parse_time(item.text("timeSpan/endTime"), wall)
        uri = item.text("mediaSegmentDescriptor/playbackURI")
        if start is None or end is None or not uri:
            continue
        query = dict(parse_qsl(urlsplit(uri).query))
        size = query.get("size")
        found.append(
            Recording(
                track=item.text("trackID") or fallback_track,
                start=start,
                end=end,
                kind=kind_of(item.text("metadataMatches/metadataDescriptor")),
                playback_uri=uri,
                name=item.text("mediaSegmentDescriptor/name") or query.get("name"),
                size=int(size) if size and size.isdigit() else None,
                locked=(item.text("mediaSegmentDescriptor/lockStatus") or "").lower() == "lock",
            )
        )
    return found, status == "MORE"


async def search(link: IsapiLink, track: str, start: datetime, end: datetime) -> list[Recording]:
    """All recordings of a track overlapping the period, oldest first.

    Some devices page by ``maxResults``, others answer everything at once and still say
    MORE; paging stops as soon as a page brings nothing new.
    """
    found: dict[tuple[str, datetime, str], Recording] = {}
    wall = await wall_offset(link)
    position = 0
    while position < MAX_RESULTS:
        body = search_body(track, start, end, position, wall)
        reply = await link.call("POST", SEARCH_PATH, body=body)
        page, more = parse_results(reply.xml(), track, wall)
        fresh = 0
        for item in page:
            key = (item.track, item.start, item.name or item.playback_uri)
            if key not in found:
                found[key] = item
                fresh += 1
        if not more or not fresh:
            break
        position += len(page)
    return sorted(
        (r for r in found.values() if r.end > start and r.start < end), key=lambda r: r.start
    )


def days_body(year: int, month: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<trackDailyParam><year>{year}</year><monthOfYear>{month}</monthOfYear></trackDailyParam>"
    )


def parse_days(document: XmlNode) -> list[int] | None:
    """Recorded days of the month, or None when the answer holds no calendar."""
    if document.first("dayList") is None:
        return None
    days: list[int] = []
    for day in document.all("dayList/day"):
        number = day.numeric("dayOfMonth")
        if number and (day.text("record") or "").lower() == "true":
            days.append(number)
    return sorted(days)


async def recorded_days(link: IsapiLink, track: str, year: int, month: int) -> list[int] | None:
    """Days of a month (device calendar) with recordings, or None if the device can't tell."""
    path = DAYS_PATH.format(track=track)
    if path in link.unsupported:
        return None
    try:
        reply = await link.call("POST", path, body=days_body(year, month))
        return parse_days(reply.xml())
    except Unsupported:
        return None
    except DeviceRejected as err:
        if err.status in (400, 404, 500, 501):
            link.unsupported.add(path)
            return None
        raise


def download_body(playback_uri: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<downloadRequest><playbackURI>{escape(playback_uri)}</playbackURI></downloadRequest>"
    )


@asynccontextmanager
async def open_download(link: IsapiLink, playback_uri: str) -> AsyncIterator[ClientResponse]:
    """Open the download of one recorded file; the body streams from the response."""
    async with link.open_stream(
        DOWNLOAD_PATH, method="GET", body=download_body(playback_uri)
    ) as response:
        yield response


@dataclass(frozen=True, slots=True)
class MediaHead:
    """What the first bytes of a download tell about it."""

    header: bool
    codec: str | None
    scrambled: bool


def inspect_head(data: bytes) -> MediaHead:
    """Look at the first video packets: codec and whether the payload is scrambled.

    Devices with stream encryption on set the PES scrambling bits of encrypted packets;
    ``scrambling`` undoes it with the device's verification code.
    """
    header = HEADER_MAGIC in data[:64]
    codec = None
    scrambled = False
    position = data.find(b"\x00\x00\x01\xe0")
    checked = 0
    while position >= 0 and checked < 8 and position + 9 < len(data):
        flags = data[position + 6]
        if flags & 0xC0 == 0x80:  # MPEG-2 PES header
            scrambled = scrambled or bool(flags & 0x30)
            start = position + 9 + data[position + 8]
            payload = data[start : start + 64]
            nal = payload.find(b"\x00\x00\x01")
            if codec is None and 0 <= nal < len(payload) - 1:
                first = payload[nal + 3]
                if (first >> 1) & 0x3F in (32, 33, 34, 35):
                    codec = "hevc"
                elif first & 0x1F in (7, 8, 9):
                    codec = "h264"
        checked += 1
        position = data.find(b"\x00\x00\x01\xe0", position + 4)
    return MediaHead(header, codec, scrambled)


def first_pts(data: bytes) -> int | None:
    """Time stamp (90 kHz) of the first video packet that has one."""
    position = data.find(b"\x00\x00\x01\xe0")
    while position >= 0 and position + 14 <= len(data):
        if data[position + 6] & 0xC0 == 0x80 and data[position + 7] & 0x80:
            raw = data[position + 9 : position + 14]
            return (
                ((raw[0] >> 1) & 0x07) << 30
                | raw[1] << 22
                | (raw[2] >> 1) << 15
                | raw[3] << 7
                | raw[4] >> 1
            )
        position = data.find(b"\x00\x00\x01\xe0", position + 4)
    return None


def starting_at(playback_uri: str, moment: datetime, wall: timedelta = _NO_OFFSET) -> str | None:
    """The URI of a recorded file asking for it from ``moment`` on; None without a start."""
    if "starttime=" not in playback_uri:
        return None
    return re.sub(r"starttime=[^&]*", f"starttime={compact(moment, wall)}", playback_uri, count=1)


def strip_header(data: bytes) -> bytes:
    """Drop the file header; downloads by time put another 24 bytes in front of it."""
    first_pack = data.find(PACK_START, 0, 1024)
    return data[first_pack:] if first_pack > 0 and HEADER_MAGIC in data[:64] else data


def file_header(data: bytes) -> bytes:
    """The file header at the start of ``data`` without the bytes before it; b"" if none.

    ffmpeg needs it first in its input: it tells ffmpeg that the audio is G.711, which it
    otherwise takes for MPEG audio, cannot decode and then waits for until the end.
    """
    at = data.find(HEADER_MAGIC, 0, 64)
    first_pack = data.find(PACK_START, 0, 1024)
    return data[at:first_pack] if 0 <= at < first_pack else b""


def for_ffmpeg(data: bytes) -> bytes:
    """The start of a download as ffmpeg reads it: from the file header on."""
    return file_header(data) + strip_header(data)
