"""Regression tests: lockout expiry, bounded multipart parts, bounded channel state."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

from custom_components.hikvision_bridge.runtime import router as router_module
from custom_components.hikvision_bridge.runtime.router import EventRouter, Origin
from custom_components.hikvision_bridge.wire import faults, feed as feed_module, link as link_module
from custom_components.hikvision_bridge.wire.feed import FeedFrame, MultipartSplitter
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink


def _link() -> IsapiLink:
    return IsapiLink(MagicMock(spec=aiohttp.ClientSession), Endpoint("h", 80, "http"), "u", "p")


def test_lockout_expires_and_clears_state() -> None:
    link = _link()
    link._signer = object()  # type: ignore[assignment]
    link._auth_checked = True
    now = [1000.0]
    with patch.object(link_module.time, "monotonic", lambda: now[0]):
        link._lock(faults.LockedOut("lock", 60))
        with pytest.raises(faults.LockedOut):
            link._check_open()
        now[0] += 61
        link._check_open()
    assert link.blocked is None
    assert link._signer is None
    assert link._auth_checked is False


def test_lockout_without_time_falls_back_to_half_an_hour() -> None:
    link = _link()
    now = [0.0]
    with patch.object(link_module.time, "monotonic", lambda: now[0]):
        link._lock(faults.LockedOut("lock"))
        now[0] = 1799
        with pytest.raises(faults.LockedOut):
            link._raise_if_blocked()
        now[0] = 1801
        link._raise_if_blocked()
    assert link.blocked is None


def test_bad_credentials_block_stays() -> None:
    link = _link()
    link._blocked = faults.BadCredentials("no")
    with (
        patch.object(link_module.time, "monotonic", lambda: 10**9),
        pytest.raises(faults.BadCredentials),
    ):
        link._raise_if_blocked()


def _part(name: bytes, body: bytes) -> bytes:
    return (
        b"--boundary\r\nContent-Type: application/xml\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
        + b"\r\n"
    )


def test_unterminated_header_block_is_bounded() -> None:
    splitter = MultipartSplitter("boundary")
    big = b"x" * (feed_module.MAX_PART_BYTES + 10)
    assert list(splitter.push(b"--boundary\r\nContent-Type: a\r\n" + big)) == []
    assert len(splitter._buffer) < 10


def test_missing_closing_boundary_is_bounded_and_resyncs() -> None:
    splitter = MultipartSplitter("boundary")
    head = b"--boundary\r\nContent-Type: text/plain\r\n\r\n"
    assert list(splitter.push(head + b"y" * (feed_module.MAX_PART_BYTES + 10))) == []
    parts = list(splitter.push(_part(b"", b"<ok/>")))
    assert len(splitter._buffer) < 100
    assert [p.body for p in parts] == [b"<ok/>"]


def test_no_boundary_no_length_is_bounded() -> None:
    splitter = MultipartSplitter(None)
    head = b"--x\r\nContent-Type: text/plain\r\n\r\n"
    assert list(splitter.push(head + b"y" * (feed_module.MAX_PART_BYTES + 10))) == []
    assert len(splitter._buffer) == 0


def _router(known: set[str]):
    bridge = SimpleNamespace(
        profile=SimpleNamespace(channels={c: object() for c in known}),
        entry=SimpleNamespace(entry_id="e"),
        link=object(),
        refresher=MagicMock(),
        ha_device_id=lambda _c: None,
    )
    hass = MagicMock()
    return EventRouter(hass, bridge, 5.0)  # type: ignore[arg-type]


def test_unknown_channel_ids_do_not_create_state() -> None:
    router = _router({"1"})
    origin = Origin("main")
    with (
        patch.object(router_module, "async_dispatcher_send"),
        patch.object(router_module, "async_call_later"),
        patch.object(router_module.recordings, "note_event"),
    ):
        for i in range(500):
            router._intake(origin, FeedFrame("VMD", "active", channel_id=str(1000 + i)))
        router._intake(origin, FeedFrame("VMD", "active", channel_id="1"))
    assert len(router.unknown_channels) <= router_module.MAX_UNKNOWN_CHANNELS
    assert set(router.recent) == {"1"}
    assert set(router._active) == {("1", "motion")} or all(s[0] == "1" for s in router._active)
    assert len(router._timers) <= 1
