"""Event stream edge cases: pictures, oversized parts, odd framing, reconnects and stops."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.wire import faults, feed
from custom_components.hikvision_bridge.wire.feed import (
    AlarmFeed,
    MultipartSplitter,
    frame_from_part,
)
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from tests.fakedevice import FakeDevice, event_xml, xml_part

pytestmark = pytest.mark.usefixtures("socket_enabled")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def test_oversized_part_is_skipped_and_stream_continues(monkeypatch) -> None:
    small = xml_part(event_xml("VMD"))
    monkeypatch.setattr(feed, "MAX_PART_BYTES", len(small) + 16)
    size = 4 * len(small)
    head = b"--boundary\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % size
    big = head + b"x" * size
    splitter = MultipartSplitter("boundary")
    parts = list(splitter.push(big[:100])) + list(splitter.push(big[100:] + b"\r\n" + small))
    assert parts[0].body == b""
    assert frame_from_part(parts[-1]).raw_type == "VMD"


def test_headers_without_leading_boundary_and_bare_newlines() -> None:
    body = event_xml("tamperdetection").encode()
    raw = b"Content-Type: application/xml\nContent-Length: %d\n\n" % len(body) + body
    (part,) = list(MultipartSplitter("boundary").push(raw))
    assert frame_from_part(part).raw_type == "tamperdetection"


def test_unparsable_and_empty_parts_are_ignored() -> None:
    splitter = MultipartSplitter("boundary")
    parts = list(splitter.push(xml_part("<broken><") + xml_part("   ") + xml_part("{bad json")))
    assert [frame_from_part(p) for p in parts] == [None, None, None]


def test_json_event_with_targets_and_port() -> None:
    payload = {
        "eventType": "linedetection",
        "eventState": "active",
        "dateTime": _now(),
        "inner": {"channelID": 3, "targetType": "human,vehicle", "inputIOPortID": 2},
    }
    (part,) = list(
        MultipartSplitter("boundary").push(xml_part(json.dumps(payload), "application/json"))
    )
    frame = frame_from_part(part)
    assert frame.channel_id == "3"
    assert frame.port_id == "2"
    assert frame.targets == ("human", "vehicle")


@pytest.fixture
async def run_feed():
    started: list = []

    async def make(device: FakeDevice, password: str | None = None):
        server = TestServer(device.app())
        await server.start_server()
        session = aiohttp.ClientSession()
        link = IsapiLink(
            session,
            Endpoint(server.host, server.port),
            device.username,
            password or device.password,
        )
        frames, states = [], []
        alarm = AlarmFeed(link, frames.append, lambda up, fault: states.append((up, fault)))
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


async def test_picture_is_attached_to_preceding_event(run_feed) -> None:
    jpeg = b"\xff\xd8picture\xff\xd9"
    picture = (
        b"--boundary\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)
        + jpeg
        + b"\r\n"
    )
    device = FakeDevice(
        stream_parts=[xml_part(event_xml("linedetection", when=_now())), picture], stream_hold=1
    )
    alarm, frames, _ = await run_feed(device)
    await _until(lambda: frames)
    assert frames[0].picture == jpeg
    assert alarm.stats.pictures == 1


async def test_stream_end_reconnects_after_backoff(run_feed, monkeypatch) -> None:
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 0.05)
    device = FakeDevice(stream_parts=[xml_part(event_xml("VMD", when=_now()))])
    alarm, frames, states = await run_feed(device)
    await _until(lambda: alarm.stats.connects >= 2)
    assert (True, None) in states
    assert (False, None) in states
    assert frames


async def test_quiet_stream_reconnects_without_dropping(run_feed, monkeypatch) -> None:
    # Door stations send no heartbeats: a quiet stream is reopened at once, not reported down.
    monkeypatch.setattr(feed, "SILENCE_LIMIT", 0.1)
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 30.0)
    device = FakeDevice(stream_parts=[xml_part(event_xml("VMD", when=_now()))], stream_hold=0.5)
    alarm, _, states = await run_feed(device)
    await _until(lambda: alarm.stats.connects >= 2)
    assert states == [(True, None)]
    assert alarm.connected


async def test_wrong_password_stops_feed_for_good(run_feed, monkeypatch) -> None:
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 0.01)
    device = FakeDevice()
    alarm, _, states = await run_feed(device, password="wrong")
    await _until(lambda: states)
    await asyncio.sleep(0.2)
    assert isinstance(states[-1][1], faults.BadCredentials)
    assert device.failed_logins == 1
    assert alarm.stats.connects == 0


async def test_failing_handler_does_not_end_stream(run_feed) -> None:
    device = FakeDevice(
        stream_parts=[
            xml_part(event_xml("VMD", when=_now())),
            xml_part(event_xml("linedetection", when=_now())),
        ],
        stream_hold=1,
    )
    seen: list[str] = []

    def handler(frame) -> None:
        seen.append(frame.raw_type)
        if frame.raw_type == "VMD":
            raise RuntimeError("handler bug")

    alarm, _, _ = await run_feed(device)
    alarm._on_frame = handler
    await _until(lambda: len(seen) == 2)
    assert seen == ["VMD", "linedetection"]
