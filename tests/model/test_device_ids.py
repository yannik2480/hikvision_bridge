"""Ids reported by the device are restricted and encoded before they reach an ISAPI path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.hikvision_bridge.model import discover
from custom_components.hikvision_bridge.model.features import io, linkage, ptz
from custom_components.hikvision_bridge.model.inventory import IoPort
from custom_components.hikvision_bridge.runtime.hookin import FreshnessGate
from custom_components.hikvision_bridge.wire.feed import BACKLOG_TOLERANCE, FeedFrame
from custom_components.hikvision_bridge.wire.xmlview import parse

NS = 'xmlns="http://www.isapi.org/ver20/XMLSchema"'


class RecordingLink:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def write_xml(self, path: str, body: str, **_: Any) -> None:
        self.paths.append(path)

    async def read_xml(self, path: str, **_: Any) -> None:
        self.paths.append(path)
        return None


def test_io_ports_with_path_tricks_are_ignored() -> None:
    doc = parse(
        f"<IOInputPortList {NS}>"
        "<IOInputPort><id>1</id></IOInputPort>"
        "<IOInputPort><id>../../Security/users</id></IOInputPort>"
        "<IOInputPort><id>1?x=1</id></IOInputPort>"
        "</IOInputPortList>"
    )
    assert list(io.parse_ports(doc, output=False)) == ["1"]
    assert list(io.parse_armed(doc)) == []  # no <enabled> given for the safe one


def test_proxy_and_local_channel_ids_must_be_numeric() -> None:
    proxy = parse(
        f"<InputProxyChannelList {NS}>"
        "<InputProxyChannel><id>2</id></InputProxyChannel>"
        "<InputProxyChannel><id>2/../../x</id></InputProxyChannel>"
        "</InputProxyChannelList>"
    )
    assert list(discover._proxy_channels(proxy, None)) == ["2"]
    local = parse(
        f"<VideoInputChannelList {NS}>"
        "<VideoInputChannel><id>1%2f..</id></VideoInputChannel>"
        "</VideoInputChannelList>"
    )
    assert discover._local_channels(local) == {}


def test_trigger_ids_are_tokens() -> None:
    doc = parse(
        f"<EventTriggerList {NS}>"
        "<EventTrigger><id>VMD-1</id><eventType>VMD</eventType></EventTrigger>"
        "<EventTrigger><id>../x</id><eventType>VMD</eventType></EventTrigger>"
        "</EventTriggerList>"
    )
    assert [t.trigger_id for t in linkage.parse_triggers(doc)] == ["VMD-1"]


@pytest.mark.asyncio
async def test_ids_are_percent_encoded_in_paths() -> None:
    link = RecordingLink()
    await io.set_output(link, IoPort(port_id="a/b?c", output=True, name=None), True)  # type: ignore[arg-type]
    await ptz.goto_preset(link, "1/../2", "3#")  # type: ignore[arg-type]
    assert link.paths[0] == "/ISAPI/System/IO/outputs/a%2Fb%3Fc/trigger"
    assert "/.." not in link.paths[1]
    assert link.paths[1].endswith("/presets/3%23/goto")


def _frame(device_time: datetime | None, raw_type: str = "VMD", state: str = "active") -> FeedFrame:
    return FeedFrame(raw_type=raw_type, state=state, device_time=device_time)


def test_webhook_drops_frames_older_than_registration() -> None:
    since = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    gate = FreshnessGate(since)
    assert not gate.admit(_frame(since - BACKLOG_TOLERANCE - timedelta(seconds=1)))
    assert gate.dropped == 1
    assert gate.admit(_frame(since - timedelta(seconds=1)))
    assert gate.admit(_frame(since + timedelta(minutes=1)))
    assert gate.admit(_frame(None))


def test_webhook_gate_uses_heartbeat_clock_offset() -> None:
    since = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    gate = FreshnessGate(since)
    beat = _frame(since + timedelta(hours=2), "videoloss", "inactive")
    beat.received = since
    assert gate.admit(beat)
    # Device clock runs two hours ahead: an event stamped at the registration instant is stale.
    assert not gate.admit(_frame(since + timedelta(hours=2) - timedelta(seconds=30)))
    assert gate.admit(_frame(since + timedelta(hours=2) + timedelta(seconds=1)))
