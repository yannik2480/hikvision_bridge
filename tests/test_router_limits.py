"""Regression test: the event router keeps no state for channels it does not know."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aiohikvision.isapi.wire.feed import FeedFrame

from custom_components.hikvision_bridge.runtime import router as router_module
from custom_components.hikvision_bridge.runtime.router import EventRouter, Origin


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
