"""Which detection events each user has read, kept on the server per user."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN

STORE_KEY = f"{DOMAIN}.event_reads"
STORE_VERSION = 1
SAVE_DELAY = 5  # seconds; a run of opened clips is written once
# Events older than this count as read. The recorder keeps its history for a shorter time
# anyway, so this bounds the stored list without any event turning unread again.
RETENTION = timedelta(days=30)
DATA_READS = f"{DOMAIN}_event_reads"


def event_key(entity_id: str, start: str) -> str:
    """Identity of a detection: its binary sensor and the start of the on-period (ISO, UTC)."""
    return f"{entity_id}|{start}"


def _start_of(key: str) -> datetime | None:
    return dt_util.parse_datetime(key.rsplit("|", 1)[1])


class EventReads:
    """Read keys per user id, saved shortly after each change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, list[str]]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._read: dict[str, set[str]] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._read = {
            user: {key for key in keys if isinstance(key, str)}
            for user, keys in stored.items()
            if isinstance(keys, list)
        }

    def is_read(self, user_id: str, entity_id: str, start: str) -> bool:
        if event_key(entity_id, start) in self._read.get(user_id, ()):
            return True
        moment = dt_util.parse_datetime(start)
        return moment is not None and moment < dt_util.utcnow() - RETENTION

    def mark(self, user_id: str, keys: list[str]) -> int:
        """Mark event keys read for a user; returns how many were unread before."""
        read = self._read.setdefault(user_id, set())
        new = {key for key in keys if key not in read}
        if new:
            read |= new
            self._store.async_delay_save(self._data, SAVE_DELAY)
        return len(new)

    def _data(self) -> dict[str, list[str]]:
        """What is stored: the keys still within the retention, sorted for a stable file."""
        cutoff = dt_util.utcnow() - RETENTION
        data: dict[str, list[str]] = {}
        for user, keys in self._read.items():
            kept = {key for key in keys if (start := _start_of(key)) and start >= cutoff}
            self._read[user] = kept
            if kept:
                data[user] = sorted(kept)
        return data


async def async_load(hass: HomeAssistant) -> EventReads:
    """Load the stored read state once at setup."""
    reads = EventReads(hass)
    await reads.async_load()
    hass.data[DATA_READS] = reads
    return reads


def get(hass: HomeAssistant) -> EventReads:
    return hass.data[DATA_READS]
