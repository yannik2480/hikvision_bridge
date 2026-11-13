"""Event stream edge cases: pictures, oversized parts, odd framing, reconnects and stops."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


FEED_LOGGER = "custom_components.hikvision_bridge.wire.feed"


async def test_stream_logs_connect_delivery_and_close(run_feed, monkeypatch, caplog) -> None:
    # A door station that closes the stream after an event: each step shows in the debug log.
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 30.0)
    caplog.set_level("DEBUG", logger=FEED_LOGGER)
    device = FakeDevice(stream_parts=[xml_part(event_xml("tamperdetection", when=_now()))])
    _, frames, _ = await run_feed(device)
    await _until(lambda: "closed by the device" in caplog.text)
    assert "connected (multipart/mixed; boundary=boundary, multipart boundary)" in caplog.text
    assert "delivered tamperdetection (active, channel 1" in caplog.text
    assert "closed by the device after 1 frame(s)" in caplog.text
    assert frames[0].raw_type == "tamperdetection"


async def test_failing_stream_logs_why_and_when_it_retries(run_feed, monkeypatch, caplog) -> None:
    # Without a log line, a stream refused on every try looks like one that never connected.
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 30.0)
    caplog.set_level("DEBUG", logger=FEED_LOGGER)
    device = FakeDevice()
    device.overrides[feed.STREAM_PATH] = (
        403,
        "<ResponseStatus><statusCode>4</statusCode>"
        "<subStatusCode>notSupport</subStatusCode></ResponseStatus>",
    )
    alarm, frames, states = await run_feed(device)
    await _until(lambda: "failed: Unsupported" in caplog.text)
    assert "retrying in 30 s" in caplog.text
    assert alarm.stats.connects == 0
    assert alarm.stats.last_error == "Unsupported"
    assert frames == []
    # Down, but not reported as an authentication fault (that would ask for new credentials).
    assert states
    assert not any(up for up, _ in states)
    assert not any(isinstance(fault, faults.BadCredentials) for _, fault in states)


async def test_backlog_drop_logs_device_clock_offset(run_feed, monkeypatch, caplog) -> None:
    # Door stations send no heartbeats, so the clock offset stays 0: an event stamped by a device
    # clock behind Home Assistant is taken for a backlog replay. The log says so.
    monkeypatch.setattr(feed, "BACKOFF_FIRST", 30.0)
    caplog.set_level("DEBUG", logger=FEED_LOGGER)
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    device = FakeDevice(stream_parts=[xml_part(event_xml("VMD", when=stamp))])
    alarm, frames, _ = await run_feed(device)
    await _until(lambda: alarm.stats.backlog_dropped == 1)
    assert frames == []
    assert "dropped VMD stamped" in caplog.text
    assert "(clock offset 0 s)" in caplog.text
