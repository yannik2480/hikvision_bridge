"""Converted recordings kept in a file while made, answered by byte ranges (see runtime/spool)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.hikvision_bridge.model.features import recordings
from custom_components.hikvision_bridge.runtime import spool
from custom_components.hikvision_bridge.views import _byte_range, recording_url
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.model.test_recordings import program_stream, result
from tests.test_playback import CLIPS, _setup, fake_ffmpeg  # noqa: F401


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spools are watched often, and an idle one stops at once."""
    monkeypatch.setattr(spool, "WATCH_SECONDS", 0.01)
    monkeypatch.setattr(spool, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(spool, "DONE_SECONDS", 0.0)


def _spools() -> spool.Spools:
    """Spools whose tasks are plain loop tasks (Home Assistant's are checked in the HTTP test)."""
    return spool.Spools(lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name))


async def _until(condition) -> None:
    for _ in range(400):  # about 2 seconds
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


async def test_range_waits_for_bytes_not_made_yet(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def produce(write) -> None:
        await write(b"abcd")
        await release.wait()
        await write(b"efgh")

    spools = _spools()
    found = spools.open("key", produce)
    try:
        with found.reading():
            await found.wait_for(1)
            assert await found.read(1, 3) == b"bcd"
            # Bytes after the ones made wait for the conversion.
            waiting = asyncio.create_task(found.wait_for(5))
            await asyncio.sleep(0.02)
            assert not waiting.done()
            release.set()
            await asyncio.wait_for(waiting, 2)
            assert await found.read(4, 4) == b"efgh"
        assert found.finished
        assert found.error is None
        assert found.written == 8
    finally:
        found.close()
    await _until(lambda: not found.path.exists())


async def test_failed_conversion_is_kept_for_readers_then_dropped() -> None:
    async def produce(write) -> None:
        await write(b"ab")
        raise ValueError("no video")

    spools = _spools()
    found = spools.open("key", produce)
    await _until(lambda: found.finished)
    assert isinstance(found.error, ValueError)
    assert found.written == 2
    # A failed spool is not handed out again: the next request converts anew.
    assert spools.get("key") is None
    assert not found.path.exists()


async def test_finished_spool_is_kept_for_its_readers(quick, monkeypatch) -> None:
    monkeypatch.setattr(spool, "DONE_SECONDS", 60.0)

    async def produce(write) -> None:
        await write(b"0123456789")

    spools = _spools()
    found = spools.open("key", produce)
    await _until(lambda: found.finished)
    assert spools.get("key") is found
    assert found.path.read_bytes() == b"0123456789"
    found.close()
    assert spools.get("key") is None
    await _until(lambda: not found.path.exists())


async def test_unread_conversion_stops_and_its_file_goes(quick) -> None:
    stopped = asyncio.Event()

    async def produce(write) -> None:
        try:
            while True:
                await write(b"x" * 16)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            stopped.set()
            raise

    spools = _spools()
    found = spools.open("key", produce)
    await _until(lambda: found.written > 0)
    await _until(stopped.is_set)
    await _until(lambda: not found.path.exists())
    assert spools.get("key") is None


async def test_read_conversion_is_not_stopped_while_read(quick) -> None:
    async def produce(write) -> None:
        for _ in range(20):
            await write(b"x")
            await asyncio.sleep(0.01)

    spools = _spools()
    found = spools.open("key", produce)
    with found.reading():
        await asyncio.sleep(0.1)
        # Read meanwhile, so it is not stopped for lack of readers.
        assert found.path.exists()
        assert not found._closed
    found.close()


async def test_least_recently_read_idle_spool_makes_room(monkeypatch) -> None:
    monkeypatch.setattr(spool, "MAX_SPOOLS", 2)

    async def produce(write) -> None:
        await asyncio.sleep(60)

    spools = _spools()
    first = spools.open("first", produce)
    second = spools.open("second", produce)
    with second.reading():
        # The first one has no reader, so it makes room for a third.
        third = spools.open("third", produce)
    assert spools.get("first") is None
    assert spools.get("second") is second
    assert spools.get("third") is third
    for found in (second, third):
        found.close()
    await _until(lambda: not first.path.exists())


async def test_full_of_read_spools_refuses_another(monkeypatch) -> None:
    monkeypatch.setattr(spool, "MAX_SPOOLS", 1)

    async def produce(write) -> None:
        await asyncio.sleep(60)

    spools = _spools()
    found = spools.open("first", produce)
    with found.reading(), pytest.raises(spool.SpoolsFull):
        spools.open("second", produce)
    found.close()


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("bytes=0-", (0, None)),
        ("bytes=10-19", (10, 19)),
        ("bytes=10-", (10, None)),
        ("bytes=-500", (None, 500)),
        ("bytes=0-1,5-9", None),  # several ranges: answered whole
        ("bytes=9-3", None),  # backwards: answered whole
        ("items=0-1", None),
        ("bytes=-", None),
    ],
)
def test_byte_range_header(header: str | None, expected: tuple | None) -> None:
    assert _byte_range(header) == expected


@pytest.mark.usefixtures("fake_ffmpeg")
async def test_streamed_mp4_answers_byte_ranges(hass, serve, hass_client) -> None:
    """Seeking: the player asks for ranges of the converted MP4, answered from the spool."""
    _, entry = await _setup(hass, serve, program_stream())
    uri = recordings.parse_results(parse(result(CLIPS, "OK")), "101")[0][0].playback_uri
    client = await hass_client()
    path = recording_url(entry.entry_id, "1", "card", "mp4", uri)

    whole = await client.get(path)
    assert whole.status == 200
    assert whole.headers["Accept-Ranges"] == "bytes"
    body = await whole.read()
    total = len(body)
    assert total > 20

    # Once made, the length is known: exact ranges with their Content-Range.
    part = await client.get(path, headers={"Range": "bytes=10-19"})
    assert part.status == 206
    assert part.headers["Content-Range"] == f"bytes 10-19/{total}"
    assert part.headers["Content-Length"] == "10"
    assert await part.read() == body[10:20]

    rest = await client.get(path, headers={"Range": "bytes=5-"})
    assert rest.status == 206
    assert rest.headers["Content-Range"] == f"bytes 5-{total - 1}/{total}"
    assert await rest.read() == body[5:]

    last = await client.get(path, headers={"Range": "bytes=-4"})
    assert last.status == 206
    assert await last.read() == body[-4:]

    beyond = await client.get(path, headers={"Range": f"bytes={total}-"})
    assert beyond.status == 416
    # Unloading the entry stops its spools and removes their files.
    spool.drop_entry(hass, entry.entry_id)
    await asyncio.sleep(0)
