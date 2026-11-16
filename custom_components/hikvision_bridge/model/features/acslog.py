"""Access control event log of door stations and access controllers.

Door stations like the DS-KD8003 keep tamper alarms, door and access events in their access
event log and do not push them over the event stream. Their own web interface reads that log
every few seconds; the integration does the same and hands each new entry on as a frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any
import uuid

from ...wire.faults import BridgeFault, DeviceRejected, Forbidden, NotIsapi, Unsupported
from ...wire.feed import FeedFrame
from ...wire.link import IsapiLink

SEARCH_PATH = "/ISAPI/AccessControl/AcsEvent?format=json"
PAGE = 30
MAX_PAGES = 10  # most entries read per poll: 300
# The whole log: devices refuse a search without a period.
_START, _END = "2000-01-01T00:00:00", "2037-12-31T23:59:59"

# (major, minor) -> (raw type, state). Codes of the Hikvision access control event list.
_KNOWN: dict[tuple[int, int], tuple[str, str]] = {
    (1, 0x400): ("zone_short_circuit", "active"),
    (1, 0x401): ("zone_broken_circuit", "active"),
    (1, 0x402): ("zone_exception", "active"),
    (1, 0x403): ("zone_restored", "inactive"),
    (1, 0x404): ("tamper", "active"),  # device opened or taken off the wall
    (1, 0x405): ("tamper", "inactive"),
    (1, 0x406): ("tamper", "active"),  # card reader opened
    (1, 0x407): ("tamper", "inactive"),
    (1, 0x408): ("tamper", "active"),  # case sensor
    (1, 0x409): ("tamper", "inactive"),
    (1, 0x40A): ("duress_alarm", "active"),
    (2, 0x27): ("nicbroken", "active"),
    (5, 37): ("doorbell", "active"),
    (5, 38): ("doorbell", "active"),
}


def frame_from_entry(info: dict[str, Any]) -> FeedFrame | None:
    """One log entry as a frame; unnamed codes keep their numbers (``acs_5_75``)."""
    major, minor = _to_int(info.get("major")), _to_int(info.get("minor"))
    if major is None or minor is None:
        return None
    raw_type, state = _KNOWN.get((major, minor), (f"acs_{major}_{minor}", "active"))
    door = info.get("doorNo")
    return FeedFrame(
        raw_type=raw_type,
        state=state,
        device_time=_device_clock(info.get("time")),
        description=f"major {major}, minor {minor}",
        extra={
            "via": "access_log",
            "major": major,
            "minor": minor,
            "serial": _to_int(info.get("serialNo")),
            "door": door if door not in (None, "") else None,
        },
    )


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _device_clock(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _order(info: dict[str, Any]) -> float:
    """Position of an entry in the log: its serial number, else its time."""
    serial = _to_int(info.get("serialNo"))
    if serial is not None:
        return float(serial)
    clock = _device_clock(info.get("time"))
    return clock.timestamp() if clock else 0.0


@dataclass(slots=True)
class LogStats:
    polls: int = 0
    entries: int = 0
    failures: int = 0
    last_error: str | None = None
    newest_first: bool | None = None


@dataclass(slots=True)
class AccessLog:
    """Reads new entries of the access event log; the first read only marks where it stands."""

    link: IsapiLink
    stats: LogStats = field(default_factory=LogStats)
    supported: bool | None = None
    _last: float | None = None
    _zone: str = ""  # some firmware only takes a period with a time zone

    async def _search(self, position: int) -> tuple[int, list[dict[str, Any]]]:
        try:
            return await self._search_once(position)
        except DeviceRejected:
            if self.supported is not None or self._zone:
                raise
            self._zone = "+00:00"
            return await self._search_once(position)

    async def _search_once(self, position: int) -> tuple[int, list[dict[str, Any]]]:
        cond = {
            "searchID": uuid.uuid4().hex,
            "searchResultPosition": position,
            "maxResults": PAGE,
            "major": 0,
            "minor": 0,
            "startTime": _START + self._zone,
            "endTime": _END + self._zone,
            "timeReverseOrder": True,
        }
        reply = await self.link.call(
            "POST",
            SEARCH_PATH,
            body=json.dumps({"AcsEventCond": cond}),
            content_type="application/json",
        )
        try:
            data = json.loads(reply.text)
        except ValueError as err:
            raise NotIsapi("AcsEvent: invalid JSON") from err
        block = data.get("AcsEvent") if isinstance(data, dict) else None
        if not isinstance(block, dict):
            raise NotIsapi("AcsEvent: no AcsEvent block")
        entries = block.get("InfoList") or []
        if not isinstance(entries, list):
            entries = []
        total = _to_int(block.get("totalMatches")) or len(entries)
        return total, [e for e in entries if isinstance(e, dict)]

    async def _newest(self) -> list[dict[str, Any]]:
        """The entries of the log, oldest first, from the newest one back to the cursor. A page
        holds PAGE entries, so a log that grew by more than that since the last poll is read
        page by page; otherwise the entries in between would never be delivered."""
        total, entries = await self._search(0)
        if len(entries) >= 2 and self.stats.newest_first is None:
            self.stats.newest_first = _order(entries[0]) > _order(entries[-1])
        if self.stats.newest_first is False:
            # The device ignores timeReverseOrder and counts from the oldest entry: the newest
            # entries are on the last page, and the pages before it hold the older ones.
            position = max(total - PAGE, 0)
            step = -PAGE
            _, entries = await self._search(position)
        else:
            position, step = 0, PAGE
        found: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            found.extend(entries)
            if not entries or self._last is None or any(_order(e) <= self._last for e in entries):
                break
            position += step
            if position < 0 or position >= total:
                break
            _, entries = await self._search(position)
        return sorted(found, key=_order)

    async def poll(self) -> list[FeedFrame]:
        """New entries since the last poll. Raises BridgeFault; Unsupported on the first poll
        means the device has no access event log."""
        self.stats.polls += 1
        try:
            entries = await self._newest()
        except (Unsupported, Forbidden, DeviceRejected, NotIsapi) as err:
            self.stats.failures += 1
            self.stats.last_error = type(err).__name__
            if self.supported is None:
                self.supported = False
                raise Unsupported("no access event log") from err
            raise
        except BridgeFault as err:
            self.stats.failures += 1
            self.stats.last_error = type(err).__name__
            raise
        self.supported = True
        newest = max((_order(e) for e in entries), default=None)
        if self._last is None:
            self._last = newest if newest is not None else 0.0
            return []
        if newest is not None and newest < self._last:
            # The log was cleared or started over: continue from where it stands now.
            self._last = newest
            return []
        fresh = [e for e in entries if _order(e) > self._last]
        if newest is not None:
            self._last = newest
        frames = [f for e in fresh if (f := frame_from_entry(e)) is not None]
        self.stats.entries += len(frames)
        return frames
