"""Event stream that gives up on devices answering 200 without a multipart body (#84).

Some door stations answer the alert stream with 200, a bare XML declaration and a closed
connection instead of a 404. aiohikvision 0.1.1 reconnects to them every few seconds. This
feed keeps the growing backoff for connections that close at once with nothing in them, and
after BARE_LIMIT such answers in a row treats the stream as unsupported, like the 404 case:
polling carries on and the stream is retried rarely. It goes once aiohikvision does the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time
from typing import Any

from aiohikvision.isapi.wire import feed
from aiohikvision.isapi.wire.faults import (
    BadCredentials,
    BridgeFault,
    LockedOut,
    Unsupported,
    detail,
)
from aiohikvision.isapi.wire.feed import AlarmFeed, boundary_of
from aiohikvision.isapi.wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

# A connection that ends sooner than this without delivering anything is no sign of a working
# stream, so it does not reset the backoff.
QUICK_CLOSE = 5.0
# Connections in a row that answer 200 without a multipart body and deliver nothing.
BARE_LIMIT = 3
# Retry interval once the stream counts as unsupported: rare checks in case the firmware changes.
UNSUPPORTED_RETRY = 3600.0
NO_STREAM = "no event stream: 200 without a multipart body"
QUICK_CLOSE_ERROR = "stream closed right after connecting"


class _WatchedLink:
    """The device link, noting whether the last stream answer had a multipart boundary."""

    def __init__(self, link: Any) -> None:
        self._link = link
        self.bare = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._link, name)

    @asynccontextmanager
    async def open_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with self._link.open_stream(*args, **kwargs) as response:
            self.bare = boundary_of(response.headers.get("Content-Type", "")) is None
            yield response


class StreamFeed(AlarmFeed):
    """AlarmFeed with a backoff for quick closes and a give-up for bare 200 answers."""

    def __init__(self, link: Any, *args: Any, **kwargs: Any) -> None:
        self._watched = _WatchedLink(link)
        super().__init__(self._watched, *args, **kwargs)  # type: ignore[arg-type]
        self._bare_streak = 0

    async def _run(self) -> None:
        delay = feed.BACKOFF_FIRST
        while True:
            frames_before = self.stats.frames
            started = time.monotonic()
            try:
                self._watched.bare = False
                went_quiet = await self._session()
                delivered = self.stats.frames > frames_before
                if self._watched.bare and not delivered:
                    self._bare_streak += 1
                else:
                    self._bare_streak = 0
                if self._bare_streak:
                    self.stats.last_error = NO_STREAM
                if self._bare_streak >= BARE_LIMIT:
                    await self._give_up_on_stream()
                    await asyncio.sleep(UNSUPPORTED_RETRY)
                    continue
                if delivered or went_quiet or time.monotonic() - started >= QUICK_CLOSE:
                    delay = feed.BACKOFF_FIRST
                elif not self._bare_streak:
                    # Closed at once with nothing in it: keep the growing backoff.
                    self.stats.last_error = QUICK_CLOSE_ERROR
                if went_quiet:
                    # Some devices (door stations) send no heartbeats: reconnect right away
                    # and stay "connected", a failing reconnect still reports the drop.
                    continue
                await self._set_state(False, None)
            except (BadCredentials, LockedOut) as err:
                self.stats.last_error = type(err).__name__
                await self._set_state(False, err)
                return
            except BridgeFault as err:
                self.stats.last_error = type(err).__name__
                _LOGGER.debug(
                    "Event stream of %s failed: %s; retrying in %d s",
                    self._link.endpoint.host,
                    detail(err),
                    delay,
                )
                await self._set_state(False, err)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.stats.last_error = type(err).__name__
                _LOGGER.exception(
                    "Event stream of %s failed unexpectedly", self._link.endpoint.host
                )
                await self._set_state(False, None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, feed.BACKOFF_MAX)

    async def _give_up_on_stream(self) -> None:
        """Stop the fast reconnects: the device has no event stream, polling carries on."""
        self.stats.last_error = NO_STREAM
        if self._bare_streak == BARE_LIMIT:
            _LOGGER.warning(
                "%s answers the event stream request without events (%d times in a row); "
                "relying on polling, retrying every %d s",
                self._link.endpoint.host,
                BARE_LIMIT,
                UNSUPPORTED_RETRY,
            )
        await self._set_state(False, Unsupported(NO_STREAM))
