"""Tests for intercom entities: unlock button, call status sensor, ringing sensor, and doorbell event."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging

from aiohttp import web
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.hikvision_bridge.const import BUS_EVENT, OPT_ACCESS_LOG_SECONDS
from custom_components.hikvision_bridge.model.vocabulary import Kind, canonical
from custom_components.hikvision_bridge.runtime import bridge as bridge_mod
from custom_components.hikvision_bridge.runtime.bridge import MAIN_FEED, DeviceBridge
from custom_components.hikvision_bridge.runtime.router import DEVICE_SCOPE
from custom_components.hikvision_bridge.wire.feed import frame_from_json, frame_from_xml
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice, event_xml, xml_part

DEVICE_INFO_INTERCOM = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<DeviceInfo version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
    "<deviceName>Door Station</deviceName>"
    "<deviceID>TESTDOORBELL</deviceID>"
    "<model>DS-KV6113-PE1</model>"
    "<serialNumber>TESTDOORBELL101</serialNumber>"
    "<macAddress>02:00:00:00:09:01</macAddress>"
    "<firmwareVersion>V3.7.0</firmwareVersion>"
    "<firmwareReleasedDate>build 240801</firmwareReleasedDate>"
    "<deviceType>VIS</deviceType>"
    "</DeviceInfo>"
)

INTERCOM_CAPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<VideoIntercomCap version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<isSupportCallStatus>true</isSupportCallStatus>"
    "<isSupportRemoteOpenDoor>true</isSupportRemoteOpenDoor>"
    "</VideoIntercomCap>"
)

DOOR_CAPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    '<doorNo min="1" max="1"/>'
    '<cmd opt="open,close"/>'
    "</RemoteControlDoor>"
)

DOOR_CAPS_MULTI = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    '<doorNo min="1" max="2"/>'
    '<cmd opt="open,close"/>'
    "</RemoteControlDoor>"
)


def _entity(hass: HomeAssistant, entry, domain: str, suffix: str) -> er.RegistryEntry:
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if item.domain == domain and item.unique_id.endswith(suffix):
            return item
    raise AssertionError(f"no {domain} entity ending in {suffix}")


async def test_intercom_entities_setup_and_unlock(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Check button
    unlock_btn = _entity(hass, entry, "button", "_open_door_1")
    assert unlock_btn is not None
    assert unlock_btn.translation_key == "open_door"

    # Check call status sensor
    call_status = _entity(hass, entry, "sensor", "_call_status")
    assert hass.states.get(call_status.entity_id).state == "idle"

    # Check ringing binary sensor
    ringing = _entity(hass, entry, "binary_sensor", "_doorbell_ringing")
    assert hass.states.get(ringing.entity_id).state == STATE_OFF

    # Check doorbell event
    bell_event = _entity(hass, entry, "event", "_doorbell")
    assert bell_event is not None

    # Door stations get the event stream too (tamper, motion); the doorbell does not depend on it
    bridge: DeviceBridge = entry.runtime_data
    assert MAIN_FEED in bridge.feeds
    assert _entity(hass, entry, "binary_sensor", "_event_link") is not None
    bridge.router._feeds_up[MAIN_FEED] = False
    bridge.router._feeds_down_since[MAIN_FEED] = 0.0
    assert not bridge.router.feeds_healthy()
    bridge.refresher.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(bell_event.entity_id).state != STATE_UNAVAILABLE
    bridge.router._feeds_up[MAIN_FEED] = True
    bridge.router._feeds_down_since.pop(MAIN_FEED)

    # Test unlocking the door via button press
    device.writes.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": unlock_btn.entity_id}, blocking=True
    )
    assert len(device.writes) == 1
    method, path, body = device.writes[0]
    assert method == "PUT"
    assert path == "/ISAPI/AccessControl/RemoteControl/door/1"
    assert "<cmd>open</cmd>" in body

    # Test fast intercom polling with ring status
    device.documents["/ISAPI/VideoIntercom/callStatus?format=json"] = json.dumps(
        {"CallStatus": {"status": "ring"}}
    )
    await bridge.async_poll_intercom()
    await hass.async_block_till_done()

    assert hass.states.get(call_status.entity_id).state == "ringing"
    assert hass.states.get(ringing.entity_id).state == STATE_ON
    assert hass.states.get(ringing.entity_id).attributes.get("device_class") is None
    assert hass.states.get(bell_event.entity_id).attributes.get("event_type") == "ring"

    # Test deduplication: calling fire_doorbell while already active does not duplicate Hit
    initial_event_time = hass.states.get(bell_event.entity_id).state
    bridge.router.fire_doorbell()
    await hass.async_block_till_done()
    assert hass.states.get(bell_event.entity_id).state == initial_event_time

    # Test fast poll back to idle
    device.documents["/ISAPI/VideoIntercom/callStatus?format=json"] = json.dumps(
        {"CallStatus": {"status": "idle"}}
    )
    bridge.router._deactivate((DEVICE_SCOPE, "doorbell"))
    await bridge.async_poll_intercom()
    await hass.async_block_till_done()

    assert hass.states.get(call_status.entity_id).state == "idle"
    assert hass.states.get(ringing.entity_id).state == STATE_OFF

    # Test call answered: when status switches to onCall, ringing sensor turns off
    device.documents["/ISAPI/VideoIntercom/callStatus?format=json"] = json.dumps(
        {"CallStatus": {"status": "ring"}}
    )
    await bridge.async_poll_intercom()
    await hass.async_block_till_done()
    assert hass.states.get(call_status.entity_id).state == "ringing"
    assert hass.states.get(ringing.entity_id).state == STATE_ON

    # Once answered (onCall) and router hold ends, ringing turns off while call_status remains on_call
    bridge.router._deactivate((DEVICE_SCOPE, "doorbell"))
    device.documents["/ISAPI/VideoIntercom/callStatus?format=json"] = json.dumps(
        {"CallStatus": {"status": "onCall"}}
    )
    await bridge.async_poll_intercom()
    await hass.async_block_till_done()

    assert hass.states.get(call_status.entity_id).state == "on_call"
    assert hass.states.get(ringing.entity_id).state == STATE_OFF

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_doorbell_vocabulary_aliases() -> None:
    assert canonical("doorbell") is Kind.DOORBELL
    assert canonical("ring") is Kind.DOORBELL
    assert canonical("ringCall") is Kind.DOORBELL
    assert canonical("bell") is Kind.DOORBELL
    # Broad aliases must not map to DOORBELL
    assert canonical("callstatus") is Kind.OTHER
    assert canonical("intercom") is Kind.OTHER
    assert canonical("videointercom") is Kind.OTHER


def test_access_controller_event_feed() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<AccessControllerEvent version="2.0">'
        "<majorEventType>5</majorEventType>"
        "<subEventType>37</subEventType>"
        "</AccessControllerEvent>"
    )
    frame = frame_from_xml(parse(xml))
    assert frame is not None
    assert frame.raw_type == "doorbell"
    assert canonical(frame.raw_type) is Kind.DOORBELL

    data = {"AccessControllerEvent": {"majorEventType": 5, "subEventType": 37}}
    frame_json = frame_from_json(data)
    assert frame_json is not None
    assert frame_json.raw_type == "doorbell"
    assert canonical(frame_json.raw_type) is Kind.DOORBELL

    # Non-doorbell access control events (majorEventType without doorbell subEventType) are dropped
    other_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<AccessControllerEvent version="2.0">'
        "<majorEventType>5</majorEventType>"
        "<subEventType>1</subEventType>"
        "<eventDescription>Card Swiped</eventDescription>"
        "</AccessControllerEvent>"
    )
    assert frame_from_xml(parse(other_xml)) is None

    other_json = {
        "AccessControllerEvent": {
            "majorEventType": 5,
            "subEventType": 1,
            "eventDescription": "Card Swiped",
        }
    }
    assert frame_from_json(other_json) is None


async def test_door_station_stream_delivers_tamper_and_motion(
    hass: HomeAssistant, serve, caplog
) -> None:
    # The door station of the report: its tamper alarm and motion come over the event stream.
    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    caplog.set_level(logging.DEBUG, logger="custom_components.hikvision_bridge.wire.feed")
    device = FakeDevice(
        "camera_g2",
        stream_parts=[
            lambda: xml_part(event_xml("tamperdetection", channel=None, when=now())),
            lambda: xml_part(event_xml("VMD", channel="1", when=now())),
        ],
        stream_hold=3,
    )
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    bus = async_capture_events(hass, BUS_EVENT)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(150):
        if {"tamper", "motion"} <= {event.data["kind"] for event in bus}:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    kinds = {event.data["kind"] for event in bus}
    assert {"tamper", "motion"} <= kinds
    assert _entity(hass, entry, "binary_sensor", "_event_link")
    link = hass.states.get(_entity(hass, entry, "binary_sensor", "_event_link").entity_id)
    assert link.state == STATE_ON
    assert "delivered tamperdetection" in caplog.text
    assert "delivered VMD" in caplog.text

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _access_log(device: FakeDevice, log: list[dict], *, reverse: bool = True) -> list[dict]:
    """An access event log answering searches like a DS-KD8003; returns the requests seen."""
    seen: list[dict] = []

    def respond(method: str, body: str) -> web.Response:
        cond = json.loads(body)["AcsEventCond"]
        seen.append(cond)
        ordered = list(reversed(log)) if reverse and cond.get("timeReverseOrder") else list(log)
        start = cond["searchResultPosition"]
        page = ordered[start : start + cond["maxResults"]]
        return web.json_response(
            {
                "AcsEvent": {
                    "searchID": cond["searchID"],
                    "responseStatusStrg": "MORE" if start + len(page) < len(log) else "OK",
                    "numOfMatches": len(page),
                    "totalMatches": len(log),
                    "InfoList": page,
                }
            }
        )

    device.responders["/ISAPI/AccessControl/AcsEvent"] = respond
    return seen


def _log_entry(serial: int, major: int, minor: int) -> dict:
    return {
        "major": major,
        "minor": minor,
        "time": f"2026-11-14T08:00:{serial % 60:02d}+01:00",
        "serialNo": serial,
        "doorNo": 1,
    }


@pytest.mark.parametrize("reverse", [True, False])
async def test_door_station_access_log_delivers_new_entries(
    hass: HomeAssistant, serve, reverse: bool
) -> None:
    # The DS-KD8003 of #44 keeps tamper alarms in its access event log, read like its web UI.
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    log = [_log_entry(n, 5, 75) for n in range(1, 41)]  # older than a page
    seen = _access_log(device, log, reverse=reverse)
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    bus = async_capture_events(hass, BUS_EVENT)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bridge: DeviceBridge = entry.runtime_data
    assert seen, "the log was not read"
    assert bridge.access_log is not None
    assert bridge.access_log.supported
    assert not [e for e in bus if e.data["raw_type"].startswith("acs_")]  # backlog skipped

    log.extend([_log_entry(41, 1, 0x404), _log_entry(42, 5, 37), _log_entry(43, 3, 0x70)])
    await bridge.async_poll_access_log()
    await hass.async_block_till_done()
    delivered = [(e.data["kind"], e.data["raw_type"], e.data["state"]) for e in bus]
    assert delivered == [("tamper", "tamper", "active"), ("other", "acs_3_112", "active")]
    assert bridge.router.is_active(DEVICE_SCOPE, Kind.TAMPER.value)

    bus.clear()
    await bridge.async_poll_access_log()
    await hass.async_block_till_done()
    assert bus == []

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_door_station_access_entries_change_the_access_event_entity(
    hass: HomeAssistant, serve
) -> None:
    # Activity of the door station: tamper and access entries of the log must show up in an
    # entity, not only on the bus; the doorbell keeps its own entity.
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    log = [_log_entry(n, 5, 75) for n in range(1, 4)]
    _access_log(device, log)
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bridge: DeviceBridge = entry.runtime_data

    log.extend([_log_entry(4, 1, 0x404), _log_entry(5, 5, 37), _log_entry(6, 3, 0x70)])
    await bridge.async_poll_access_log()
    await hass.async_block_till_done()

    access = _entity(hass, entry, "event", "_access_events")
    state = hass.states.get(access.entity_id)
    assert state.state != STATE_UNAVAILABLE
    assert state.attributes["event_type"] == "access"
    assert state.attributes["raw_type"] == "acs_3_112"  # the last one, the bell is not one
    assert state.attributes["kind"] == "other"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_door_station_stream_access_event_reaches_the_access_event_entity(
    hass: HomeAssistant, serve, caplog
) -> None:
    # AccessControllerEvent over the event stream of a door station (channel 1, no port).
    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    caplog.set_level(logging.DEBUG, logger="custom_components.hikvision_bridge.wire.feed")
    device = FakeDevice(
        "camera_g2",
        stream_parts=[
            lambda: xml_part(event_xml("AccessControllerEvent", channel="1", when=now())),
        ],
        stream_hold=3,
    )
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    bus = async_capture_events(hass, BUS_EVENT)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(150):
        if bus:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    assert "delivered AccessControllerEvent" in caplog.text
    assert [(e.data["channel"], e.data["kind"]) for e in bus] == [(None, "other")]
    access = _entity(hass, entry, "event", "_access_events")
    state = hass.states.get(access.entity_id)
    assert state.attributes["raw_type"] == "AccessControllerEvent"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_access_log_stops_on_devices_without_one(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
        }
    )
    device.responders["/ISAPI/AccessControl/AcsEvent"] = lambda method, body: web.Response(
        status=404, text="<ResponseStatus><statusCode>4</statusCode></ResponseStatus>"
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bridge: DeviceBridge = entry.runtime_data
    assert bridge.access_log is not None
    assert bridge.access_log.supported is False
    assert bridge._access_log_unsub is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(("options", "seconds"), [({}, 5), ({OPT_ACCESS_LOG_SECONDS: 7}, 7)])
async def test_access_log_interval_follows_the_option(
    hass: HomeAssistant, serve, monkeypatch, options: dict, seconds: int
) -> None:
    scheduled: list[timedelta] = []
    real = bridge_mod.async_track_time_interval

    def spy(hass_, action, interval):
        scheduled.append(interval)
        return real(hass_, action, interval)

    monkeypatch.setattr(bridge_mod, "async_track_time_interval", spy)
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS,
        }
    )
    server = await serve(device)
    entry = device_entry(server, device, options=options)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert timedelta(seconds=seconds) in scheduled
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_intercom_multi_door(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("camera_g2")
    device.documents.update(
        {
            "/ISAPI/System/deviceInfo": DEVICE_INFO_INTERCOM,
            "/ISAPI/VideoIntercom/capabilities": INTERCOM_CAPS,
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": DOOR_CAPS_MULTI,
            "/ISAPI/VideoIntercom/callStatus?format=json": json.dumps(
                {"CallStatus": {"status": "idle"}}
            ),
        }
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    door1 = _entity(hass, entry, "button", "_open_door_1")
    door2 = _entity(hass, entry, "button", "_open_door_2")
    assert door1 is not None
    assert door2 is not None
    assert door1.translation_key == "open_door_numbered"
    assert door2.translation_key == "open_door_numbered"

    device.writes.clear()
    await hass.services.async_call("button", "press", {"entity_id": door2.entity_id}, blocking=True)
    assert len(device.writes) == 1
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", "/ISAPI/AccessControl/RemoteControl/door/2")
    assert "<cmd>open</cmd>" in body

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
