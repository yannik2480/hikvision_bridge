"""The camera's audible alarm is a siren entity; on plays the sound until it is turned off."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from homeassistant.components.siren import SirenEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.hikvision_bridge import siren
from custom_components.hikvision_bridge.model.features.image import ImageCapability
from custom_components.hikvision_bridge.model.inventory import ChannelProfile, ChannelSource
from custom_components.hikvision_bridge.wire.faults import DeviceRejected, Forbidden
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice

CAPS_PATH = "/ISAPI/Event/triggers/notifications/AudioAlarm/capabilities?format=json"
CONFIG_PATH = "/ISAPI/Event/triggers/notifications/AudioAlarm?format=json"
TEST_RESOURCE = "/ISAPI/Event/triggers/notifications/AudioAlarm/{sound}/test"
TEST_PATH = TEST_RESOURCE + "?format=json"

AUDIO_CAPS = {
    "AudioAlarmCap": {
        "AlertAudioTypeListCap": [
            {"alertAudioID": 1, "alertAudioDescription": "Siren"},
            {"alertAudioID": 2, "alertAudioDescription": "Warning,this is a restricted area"},
            {"alertAudioID": 3, "alertAudioDescription": "Audio Warning"},
        ],
        "audioVolume": {"@min": 1, "@max": 100},
        "alarmTimes": {"@min": 1, "@max": 50},
    }
}

BUSY = (
    "<ResponseStatus><statusCode>6</statusCode><statusString>Invalid Operation</statusString>"
    "<subStatusCode>audioIsPlayingPleaseWait</subStatusCode></ResponseStatus>"
)
REFUSED = (
    "<ResponseStatus><statusCode>4</statusCode><subStatusCode>deviceError</subStatusCode>"
    "</ResponseStatus>"
)


def _siren_device(*, alert_id: int = 3) -> FakeDevice:
    device = FakeDevice("camera_g3")
    device.documents.update(
        {
            CAPS_PATH: json.dumps(AUDIO_CAPS),
            CONFIG_PATH: json.dumps(
                {
                    "AudioAlarm": {
                        "audioClass": "alertAudio",
                        "alertAudioID": alert_id,
                        "audioID": alert_id,
                        "audioVolume": 70,
                        "alarmTimes": 3,
                    }
                }
            ),
        }
    )
    return device


def _answer_test(device: FakeDevice, sound: int, responder) -> None:
    device.responders[TEST_RESOURCE.format(sound=sound)] = responder


def _reply(status: int, text: str):
    return lambda method, body: web.Response(
        status=status, text=text, content_type="application/xml"
    )


def _sirens(hass: HomeAssistant, entry) -> list[str]:
    return [
        item.entity_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == "siren"
    ]


def _trigger_writes(device: FakeDevice) -> list[str]:
    return [path for method, path, _ in device.writes if method == "PUT" and "/test" in path]


async def _wait_for(predicate) -> None:
    for _ in range(500):  # about five seconds
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def _set_up(hass: HomeAssistant, serve, device: FakeDevice):
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tear_down(hass: HomeAssistant, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def fast_retrigger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(siren, "RETRIGGER_SECONDS", 0.01)


def test_channel_has_siren_only_with_audio_alarm_capabilities() -> None:
    channel = ChannelProfile("1", "CCTV", ChannelSource.LOCAL)
    assert not channel.siren
    channel.image["strobe_duration"] = ImageCapability("strobe_duration", minimum=1, maximum=60)
    assert not channel.siren
    channel.image["siren_volume"] = ImageCapability("siren_volume", minimum=1, maximum=100)
    assert channel.siren


def test_busy_refusal_counts_as_still_playing() -> None:
    assert siren._still_playing(DeviceRejected("audio is playing", 403, "audioIsPlayingPleaseWait"))
    assert not siren._still_playing(DeviceRejected("HTTP 500", 500, None))
    assert not siren._still_playing(Forbidden("HTTP 403"))


async def test_siren_plays_the_sound_the_setting_shows(hass: HomeAssistant, serve) -> None:
    # alertAudioID is the value the siren sound setting reads, so it is the sound played.
    device = _siren_device(alert_id=2)
    entry = await _set_up(hass, serve, device)

    (entity,) = _sirens(hass, entry)
    unique_id = er.async_get(hass).async_get(entity).unique_id
    assert unique_id.endswith("_ch1_siren")
    state = hass.states.get(entity)
    assert state.state == STATE_OFF
    assert state.attributes["supported_features"] == (
        SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    )

    device.writes.clear()
    await hass.services.async_call("siren", "turn_on", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_ON
    assert device.writes == [("PUT", TEST_PATH.format(sound=2), "{}")]

    await hass.services.async_call("siren", "turn_off", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_OFF
    await _tear_down(hass, entry)


async def test_siren_repeats_until_turned_off(
    hass: HomeAssistant, serve, fast_retrigger: None
) -> None:
    device = _siren_device()
    entry = await _set_up(hass, serve, device)
    (entity,) = _sirens(hass, entry)

    await hass.services.async_call("siren", "turn_on", {"entity_id": entity}, blocking=True)
    await _wait_for(lambda: len(_trigger_writes(device)) >= 3)
    assert hass.states.get(entity).state == STATE_ON

    await hass.services.async_call("siren", "turn_off", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_OFF
    stopped_at = len(_trigger_writes(device))
    await asyncio.sleep(0.1)
    assert len(_trigger_writes(device)) == stopped_at
    await _tear_down(hass, entry)


async def test_busy_camera_keeps_the_siren_on(
    hass: HomeAssistant, serve, fast_retrigger: None
) -> None:
    device = _siren_device()
    _answer_test(device, 3, _reply(403, BUSY))
    entry = await _set_up(hass, serve, device)
    (entity,) = _sirens(hass, entry)

    # The first trigger is refused as busy too: the siren is sounding already.
    await hass.services.async_call("siren", "turn_on", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_ON
    await _wait_for(lambda: len(_trigger_writes(device)) >= 4)
    assert hass.states.get(entity).state == STATE_ON

    await hass.services.async_call("siren", "turn_off", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_OFF
    await _tear_down(hass, entry)


async def test_refused_first_trigger_reports_an_error(hass: HomeAssistant, serve) -> None:
    device = _siren_device()
    _answer_test(device, 3, _reply(500, REFUSED))
    entry = await _set_up(hass, serve, device)
    (entity,) = _sirens(hass, entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("siren", "turn_on", {"entity_id": entity}, blocking=True)
    assert hass.states.get(entity).state == STATE_OFF
    await _tear_down(hass, entry)


async def test_siren_gives_up_after_repeated_failures(
    hass: HomeAssistant, serve, fast_retrigger: None
) -> None:
    device = _siren_device()
    calls = []

    def answer(method: str, body: str) -> web.Response:
        calls.append(method)
        if len(calls) == 1:
            return web.Response(text="{}", content_type="application/json")
        return web.Response(status=500, text=REFUSED, content_type="application/xml")

    _answer_test(device, 3, answer)
    entry = await _set_up(hass, serve, device)
    (entity,) = _sirens(hass, entry)

    await hass.services.async_call("siren", "turn_on", {"entity_id": entity}, blocking=True)
    await _wait_for(lambda: hass.states.get(entity).state == STATE_OFF)
    assert len(calls) == 1 + siren.MAX_FAILURES
    await _tear_down(hass, entry)


async def test_no_siren_without_audio_alarm_capabilities(hass: HomeAssistant, serve) -> None:
    for device in (FakeDevice("camera_g3"), FakeDevice("nvr")):
        entry = await _set_up(hass, serve, device)
        assert _sirens(hass, entry) == []
        await _tear_down(hass, entry)
