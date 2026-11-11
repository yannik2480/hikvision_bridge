"""Plate cameras: plate sensor, pictures and event details; services on a failed setup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from unittest.mock import patch

from homeassistant.components.image import async_get_image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.hikvision_bridge.const import BUS_EVENT, DOMAIN
from custom_components.hikvision_bridge.diagnostics import async_get_config_entry_diagnostics
from custom_components.hikvision_bridge.runtime.refresh import StatusRefresher
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice, anpr_xml, picture_part, xml_part

CAPS = "/ISAPI/Event/channels/capabilities"


def _entity_id(hass: HomeAssistant, entry, domain: str, suffix: str) -> str:
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if item.domain == domain and item.unique_id.endswith(suffix):
            return item.entity_id
    raise AssertionError(f"no {domain} entity ending in {suffix}")


def _plate_recorder(stream_parts: list[bytes]) -> FakeDevice:
    """The recorder fixture with a plate camera on channel 1."""
    device = FakeDevice("nvr", stream_parts=stream_parts, stream_hold=2, stream_gap=0.05)
    caps = device._index[CAPS][1]
    device.documents[CAPS] = caps.replace('<eventType opt="', '<eventType opt="ANPR,', 1)
    return device


async def test_plate_reaches_sensor_pictures_event_and_bus(hass: HomeAssistant, serve) -> None:
    plate_jpg, scene_jpg = b"\xff\xd8plate\xff\xd9", b"\xff\xd8scene\xff\xd9"

    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    device = _plate_recorder(
        [
            lambda: xml_part(anpr_xml(when=now())),
            picture_part(plate_jpg, "licensePlatePicture.jpg"),
            picture_part(scene_jpg, "detectionPicture.jpg"),
        ]
    )
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    bus = async_capture_events(hass, BUS_EVENT)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensor = _entity_id(hass, entry, "sensor", "_ch1_license_plate")
    scene = _entity_id(hass, entry, "image", "_ch1_anpr_scene_picture")
    for _ in range(150):
        if bus:
            break
        await asyncio.sleep(0.02)
    await hass.async_block_till_done()

    assert bus[0].data["kind"] == "anpr"
    assert bus[0].data["anpr"]["plate"] == "B AB 1234"
    state = hass.states.get(sensor)
    assert state.state == "B AB 1234"
    assert state.attributes["confidence"] == 97
    assert state.attributes["list"] == "allow"
    event = hass.states.get(_entity_id(hass, entry, "event", "_ch1_detection_events"))
    assert event.attributes["event_type"] == "anpr"
    assert event.attributes["plate"] == "B AB 1234"
    assert event.attributes["vehicle_color"] == "blue"
    assert (await async_get_image(hass, scene)).content == scene_jpg
    # Plates the camera read never end up in a diagnostics download.
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "B AB 1234" not in json.dumps(diagnostics, default=str)
    # The close-up exists but is off by default; channels without ANPR get no plate entities.
    crop = er.async_get(hass).async_get(_entity_id(hass, entry, "image", "_ch1_anpr_plate_picture"))
    assert crop.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    with pytest.raises(AssertionError):
        _entity_id(hass, entry, "sensor", "_ch2_license_plate")

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_failed_first_refresh_leaves_no_bridge_behind(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial101")

    with patch.object(StatusRefresher, "_async_update_data", side_effect=UpdateFailed("down")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert getattr(entry, "runtime_data", None) is None
    target = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "testserial101"), entry.entry_id
    )
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "isapi_request",
            {"device_id": target.id, "path": "/ISAPI/System/deviceInfo"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "device_not_ready"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
