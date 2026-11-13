"""Tests for intercom entities: unlock button, call status sensor, ringing sensor, and doorbell event."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.hikvision_bridge.const import BUS_EVENT
from custom_components.hikvision_bridge.model.vocabulary import Kind, canonical
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
