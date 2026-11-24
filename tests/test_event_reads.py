"""Read and unread state of detection events: per user, kept on the server."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hikvision_bridge import websocket
from custom_components.hikvision_bridge.const import DOMAIN
from custom_components.hikvision_bridge.runtime import event_reads
from tests.test_panel import _setup

ENTITY = "binary_sensor.hikvision_motion_1"
NOW = dt_util.utcnow().replace(microsecond=0)
FIRST = NOW - timedelta(hours=2)
SECOND = NOW - timedelta(hours=1)


def _event(start: datetime) -> dict:
    return {
        "entity_id": ENTITY,
        "kind": "motion",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=1)).isoformat(),
    }


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Two detections in the last hours, in place of the recorder history."""
    found = [_event(FIRST), _event(SECOND)]

    async def window(hass, entities, start, end):
        return [dict(e) for e in found if start <= dt_util.parse_datetime(e["start"]) < end]

    monkeypatch.setattr(
        websocket, "_event_entities", lambda hass, bridge, channel: {ENTITY: "motion"}
    )
    monkeypatch.setattr(websocket, "_events", window)
    return found


async def _timeline(client, entry_id: str) -> list[dict]:
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/timeline",
            "entry_id": entry_id,
            "channel_id": "1",
            "source": "auto",
            "start": (NOW - timedelta(hours=3)).isoformat(),
            "end": NOW.isoformat(),
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    return answer["result"]["events"]


async def test_events_are_read_per_user(
    hass: HomeAssistant, serve, hass_ws_client, hass_read_only_access_token, events
) -> None:
    _, entry = await _setup(hass, serve)
    admin = await hass_ws_client(hass)
    other = await hass_ws_client(hass, hass_read_only_access_token)
    assert [e["read"] for e in await _timeline(admin, entry.entry_id)] == [False, False]

    await admin.send_json_auto_id(
        {
            "type": f"{DOMAIN}/events/read",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "events": [{"entity_id": ENTITY, "start": FIRST.isoformat()}],
        }
    )
    assert (await admin.receive_json())["success"]
    assert [e["read"] for e in await _timeline(admin, entry.entry_id)] == [True, False]
    assert [e["read"] for e in await _timeline(other, entry.entry_id)] == [False, False]


async def test_events_read_refuses_what_is_not_an_event(
    hass: HomeAssistant, serve, hass_ws_client, events
) -> None:
    _, entry = await _setup(hass, serve)
    client = await hass_ws_client(hass)
    base = {"type": f"{DOMAIN}/events/read", "entry_id": entry.entry_id, "channel_id": "1"}

    await client.send_json_auto_id(
        base | {"events": [{"entity_id": "switch.other", "start": FIRST.isoformat()}]}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"
    await client.send_json_auto_id(base | {"events": [{"entity_id": ENTITY, "start": "soon"}]})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"
    await client.send_json_auto_id(base | {"events": []})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"
    await client.send_json_auto_id(
        base | {"entry_id": "nope", "events": [{"entity_id": ENTITY, "start": FIRST.isoformat()}]}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def test_read_all_marks_the_window(
    hass: HomeAssistant, serve, hass_ws_client, events
) -> None:
    _, entry = await _setup(hass, serve)
    client = await hass_ws_client(hass)
    window = {
        "type": f"{DOMAIN}/events/read_all",
        "entry_id": entry.entry_id,
        "channel_id": "1",
        "start": (NOW - timedelta(minutes=90)).isoformat(),
        "end": NOW.isoformat(),
    }
    await client.send_json_auto_id(window)
    assert (await client.receive_json())["result"] == {"marked": 1}
    await client.send_json_auto_id(window)
    assert (await client.receive_json())["result"] == {"marked": 0}
    assert [e["read"] for e in await _timeline(client, entry.entry_id)] == [False, True]

    await client.send_json_auto_id(window | {"start": "later"})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"


async def test_reads_are_stored_and_older_events_count_as_read(
    hass: HomeAssistant, hass_storage
) -> None:
    reads = await event_reads.async_load(hass)
    recent = event_reads.event_key(ENTITY, FIRST.isoformat())
    long_ago = (NOW - event_reads.RETENTION - timedelta(days=1)).isoformat()

    assert reads.mark("user", [recent]) == 1
    assert reads.mark("user", [recent]) == 0
    assert reads.is_read("user", ENTITY, FIRST.isoformat())
    assert not reads.is_read("user", ENTITY, SECOND.isoformat())
    assert not reads.is_read("someone", ENTITY, FIRST.isoformat())
    # Past the retention every event is read, so nothing that is dropped reappears as unread.
    assert reads.is_read("someone", ENTITY, long_ago)

    reads.mark("user", [event_reads.event_key(ENTITY, long_ago)])
    assert reads._data() == {"user": [recent]}
    await reads._store.async_save(reads._data())
    assert hass_storage[event_reads.STORE_KEY]["data"] == {"user": [recent]}

    again = await event_reads.async_load(hass)
    assert again.is_read("user", ENTITY, FIRST.isoformat())
    assert not again.is_read("user", ENTITY, SECOND.isoformat())
