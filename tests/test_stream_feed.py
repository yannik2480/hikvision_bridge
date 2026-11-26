"""Event stream of devices that answer 200 without events, or close at once (#84)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiohikvision.isapi.wire import faults, feed
from aiohikvision.isapi.wire.link import Endpoint, IsapiLink
import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.runtime import stream_feed
from custom_components.hikvision_bridge.runtime.stream_feed import StreamFeed
from tests.fakedevice import FakeDevice, event_xml, xml_part

pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def run_feed():
    started: list = []

    async def make(device: FakeDevice):
        server = TestServer(device.app())
        await server.start_server()
        session = aiohttp.ClientSession()
        link = IsapiLink(
            session, Endpoint(server.host, server.port), device.username, device.password
        )
        frames, states = [], []
        alarm = StreamFeed(link, frames.append, lambda up, fault: states.append((up, fault)))
        alarm.start(lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name))
        started.append((alarm, session, server))
        return alarm, frames, states

    yield make
    for alarm, session, server in started:
        await alarm.stop()
        await session.close()
        await server.close()


async def _until(condition, seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_bare_200_without_multipart_gives_up_after_three_closes(
    run_feed, monkeypatch
) -> None:
    # Door stations answer 200 with only an XML declaration and close at once (fw V2.2.x).
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 0.01)
    monkeypatch.setattr(stream_feed, "UNSUPPORTED_RETRY", 60.0)
    body = '<?xml version="1.0" encoding="UTF-8"?>'
    device = FakeDevice(overrides={feed.STREAM_PATH: (200, body)})
    alarm, frames, states = await run_feed(device)
    await _until(lambda: bool(states) and isinstance(states[-1][1], faults.Unsupported))
    connects = alarm.stats.connects
    await asyncio.sleep(0.2)
    # Three quick connections, then the long retry: no more reconnects in a tight loop.
    assert connects == alarm.stats.connects == 3
    assert alarm.stats.last_error == stream_feed.NO_STREAM
    assert not alarm.connected
    assert not frames


async def test_quick_closes_back_off_instead_of_retrying_at_the_first_delay(
    run_feed, monkeypatch
) -> None:
    # A multipart stream that closes at once with nothing in it is not "working": the backoff
    # keeps growing (0.2 s, 0.4 s, ...) rather than reconnecting every 0.2 s.
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 0.2)
    device = FakeDevice()
    alarm, frames, _ = await run_feed(device)
    await _until(lambda: alarm.stats.connects >= 1)
    await asyncio.sleep(0.5)
    assert 1 <= alarm.stats.connects <= 2
    assert alarm.stats.last_error == stream_feed.QUICK_CLOSE_ERROR
    assert not frames


async def test_stream_with_events_still_delivers(run_feed) -> None:
    device = FakeDevice(
        stream_parts=[
            xml_part(event_xml("VMD", when=datetime.now(UTC).isoformat(timespec="seconds")))
        ],
        stream_hold=1,
    )
    alarm, frames, _ = await run_feed(device)
    await _until(lambda: frames)
    assert frames[0].raw_type == "VMD"
    assert alarm.stats.last_error is None
