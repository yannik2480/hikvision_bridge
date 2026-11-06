"""Events from the alert stream reach entities; switches change only the enabled flag."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.hikvision_bridge.const import BUS_EVENT, OPT_HOLD_SECONDS
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice, event_xml, xml_part


def _entity_id(hass: HomeAssistant, entry, domain: str, suffix: str) -> str:
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if item.domain == domain and item.unique_id.endswith(suffix):
            return item.entity_id
    raise AssertionError(f"no {domain} entity ending in {suffix}")


async def test_line_crossing_turns_sensor_on_and_fires_event(hass: HomeAssistant, serve) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    device = FakeDevice(
        "nvr",
        stream_parts=[xml_part(event_xml("linedetection", channel="1", when=now))],
        stream_hold=2,
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={OPT_HOLD_SECONDS: 60})
    bus = async_capture_events(hass, BUS_EVENT)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(100):  # the feed runs in the background over a real socket
        if bus:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    sensor = _entity_id(hass, entry, "binary_sensor", "_ch1_event_line_crossing")
    assert hass.states.get(sensor).state == STATE_ON
    event = _entity_id(hass, entry, "event", "_ch1_detection_events")
    assert hass.states.get(event).attributes["event_type"] == "line_crossing"
    assert bus
    assert bus[0].data["kind"] == "line_crossing"
    # another channel stays untouched
    other = _entity_id(hass, entry, "binary_sensor", "_ch4_event_motion")
    assert hass.states.get(other).state == STATE_OFF

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_illegal_login_turns_device_sensor_on(hass: HomeAssistant, serve) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    device = FakeDevice(
        "camera_g3",
        stream_parts=[xml_part(event_xml("illAccess", channel="1", when=now))],
        stream_hold=2,
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={OPT_HOLD_SECONDS: 60})
    bus = async_capture_events(hass, BUS_EVENT)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(100):  # the feed runs in the background over a real socket
        if bus:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    sensor = _entity_id(hass, entry, "binary_sensor", "_illegal_login")
    assert hass.states.get(sensor).state == STATE_ON
    assert bus[0].data["kind"] == "illegal_access"
    assert bus[0].data["channel"] is None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_inactive_report_keeps_detection_on_for_the_hold(hass: HomeAssistant, serve) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    device = FakeDevice(
        "nvr",
        stream_parts=[
            xml_part(event_xml("fielddetection", channel="1", when=now)),
            xml_part(event_xml("fielddetection", "inactive", channel="1", when=now)),
        ],
        stream_hold=2,
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={OPT_HOLD_SECONDS: 60})
    bus = async_capture_events(hass, BUS_EVENT)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(100):  # the feed runs in the background over a real socket
        if len(bus) >= 2:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    assert [event.data["state"] for event in bus] == ["active", "inactive"]
    sensor = _entity_id(hass, entry, "binary_sensor", "_ch1_event_intrusion")
    assert hass.states.get(sensor).state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_switch_writes_back_only_the_enabled_flag(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    switch = _entity_id(hass, entry, "switch", "_ch1_switch_line_crossing")
    before = hass.states.get(switch).state
    service = "turn_off" if before == STATE_ON else "turn_on"
    await hass.services.async_call("switch", service, {"entity_id": switch}, blocking=True)

    assert len(device.writes) == 1
    method, path, body = device.writes[0]
    assert method == "PUT"
    assert path == "/ISAPI/Smart/LineDetection/1"
    original = device._index[path][1]
    flipped = "false" if before == STATE_ON else "true"
    assert f"<enabled>{flipped}</enabled>" in body
    # everything except the flag is byte-identical
    assert body.replace(f"<enabled>{flipped}</enabled>", "", 1) == original.replace(
        f"<enabled>{'true' if flipped == 'false' else 'false'}</enabled>", "", 1
    )

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
