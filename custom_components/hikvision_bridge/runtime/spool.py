"""Converted recordings kept in a temporary file while they are made, for byte ranges.

A browser seeks in a video by asking for a byte range of it. A streamed MP4 is made as it
downloads, so its bytes exist only once they are made: the converted output is written to a
file as it comes, and every request for a range of it is answered from that file. Bytes
made already come at once; bytes not made yet are waited for. A spool stops being made when
no player reads it for a while, and its file is removed then (or once it is kept long enough
after it was finished).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable, Iterator
import contextlib
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
from typing import Final

# Seconds without a reader after which a spool that is still being made stops.
IDLE_SECONDS: Final = 60
# Seconds a finished spool is kept after its last reader.
DONE_SECONDS: Final = 600
# Spools at the same time; the least recently read one without a reader makes room.
MAX_SPOOLS: Final = 4
WATCH_SECONDS: Final = 5

Write = Callable[[bytes], Awaitable[None]]
Produce = Callable[[Write], Awaitable[None]]


class SpoolsFull(Exception):
    """MAX_SPOOLS recordings are made at the moment; none is idle to make room."""


def _read_at(path: Path, position: int, size: int) -> bytes:
    with path.open("rb") as file:
        file.seek(position)
        return file.read(size)


class Spool:
    """The output of one conversion, written to a file as it is made.

    ``produce`` gets a ``write`` callable and makes the output; an exception it raises is kept
    in ``error`` for the readers. ``finished`` is set once no more bytes come.
    """

    def __init__(
        self,
        key: Hashable,
        path: Path,
        produce: Produce,
        on_close: Callable[[Spool], None],
    ) -> None:
        self.key = key
        self.path = path
        self.written = 0
        self.finished = False
        self.error: Exception | None = None
        self.readers = 0
        self.used = time.monotonic()
        self._changed = asyncio.Event()
        self._produce = produce
        self._on_close = on_close
        self._writer = None
        self._started = False
        self._closed = False
        loop = asyncio.get_running_loop()
        self._producer = loop.create_task(self._run(), name="hikvision_bridge spool")
        self._watcher = loop.create_task(self._watch(), name="hikvision_bridge spool watch")

    # ------------------------------------------------------------- making

    async def _run(self) -> None:
        self._started = True
        try:
            self._writer = await asyncio.to_thread(self.path.open, "wb")
            await self._produce(self._write)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - kept for the readers, who answer with it
            self.error = err
        finally:
            if self._writer is not None:
                self._writer.close()
            self._finish()
            if self._closed:
                self._remove()

    async def _write(self, data: bytes) -> None:
        if self._closed or self._writer is None:
            return  # closed: the producer is cancelled and ends
        await asyncio.to_thread(self._write_bytes, data)
        self.written += len(data)
        self._notify()

    def _write_bytes(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        self._writer.flush()

    def _finish(self) -> None:
        self.finished = True
        self._notify()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _remove(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            if self.readers:
                continue
            idle = time.monotonic() - self.used
            if (not self.finished and idle > IDLE_SECONDS) or (
                self.finished and idle > DONE_SECONDS
            ):
                self.close()
                return

    # ------------------------------------------------------------- reading

    @contextmanager
    def reading(self) -> Iterator[Spool]:
        """Marks the spool as read while a response uses it; idle spools stop being made."""
        self.readers += 1
        try:
            yield self
        finally:
            self.readers -= 1
            self.used = time.monotonic()

    async def wait_for(self, position: float) -> None:
        """Returns once more than ``position`` bytes are made, or none more will be."""
        while self.written <= position and not self.finished:
            changed = self._changed
            await changed.wait()

    async def read(self, position: int, size: int) -> bytes:
        return await asyncio.to_thread(_read_at, self.path, position, size)

    # ------------------------------------------------------------- closing

    def close(self) -> None:
        """Stops making the output and removes its file once nothing writes to it."""
        if self._closed:
            return
        self._closed = True
        self._on_close(self)
        if self._watcher is not asyncio.current_task():
            self._watcher.cancel()
        self._producer.cancel()
        if self._producer.done() or not self._started:
            # Nothing runs to remove the file: the producer ended, or never started.
            self._remove()


class Spools:
    """The spools in use, by key; a finished one with an error is dropped for a new try."""

    def __init__(self) -> None:
        self._spools: dict[Hashable, Spool] = {}

    def get(self, key: Hashable) -> Spool | None:
        spool = self._spools.get(key)
        if spool is not None and spool.error is not None:
            spool.close()
            return None
        return spool

    def open(self, key: Hashable, produce: Produce) -> Spool:
        """A new spool for ``key``, started now; SpoolsFull if none can be dropped."""
        if len(self._spools) >= MAX_SPOOLS:
            idle = [spool for spool in self._spools.values() if not spool.readers]
            if not idle:
                raise SpoolsFull
            min(idle, key=lambda spool: spool.used).close()
        descriptor, name = tempfile.mkstemp(prefix="hikvision_bridge_", suffix=".mp4")
        os.close(descriptor)
        spool = Spool(key, Path(name), produce, self._forget)
        self._spools[key] = spool
        return spool

    def _forget(self, spool: Spool) -> None:
        if self._spools.get(spool.key) is spool:
            del self._spools[spool.key]
