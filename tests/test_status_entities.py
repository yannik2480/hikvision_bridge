"""Recording, device status, address and armable inputs of a recorder."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from tests.conftest import device_entry
from tests.fakedevice import FakeDevice

NS = 'version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"'
STATUS = (
    f"<DeviceStatus {NS}><deviceUpTime>3600</deviceUpTime>"
    "<CPUList><CPU><cpuUtilization>12</cpuUtilization></CPU></CPUList>"
    "<totalRebootCount>4</totalRebootCount></DeviceStatus>"
)
INPUT_1 = f"<IOInputPort {NS}><id>1</id><enabled>false</enabled><triggering>high</triggering></IOInputPort>"


def _entity(hass: HomeAssistant, entry, domain: str, suffix: str) -> er.RegistryEntry:
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if item.domain == domain and item.unique_id.endswith(suffix):
            return item
    raise AssertionError(f"no {domain} entity ending in {suffix}")


async def test_recorder_status_entities(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    device.documents.update({"/ISAPI/System/status": STATUS, "/ISAPI/System/IO/inputs/1": INPUT_1})
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    recording = _entity(hass, entry, "binary_sensor", "_ch1_recording")
    assert hass.states.get(recording.entity_id).state == STATE_ON

    address = _entity(hass, entry, "sensor", "_network_address")
    assert hass.states.get(address.entity_id).state == server.host

    # Device status is diagnostic and starts disabled.
    for feature in ("boot_time", "cpu_load", "reboots"):
        assert _entity(hass, entry, "sensor", f"_{feature}").disabled_by is not None

    armed = _entity(hass, entry, "switch", "_armed_1")
    assert hass.states.get(armed.entity_id).state == STATE_OFF
    device.writes.clear()
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": armed.entity_id}, blocking=True
    )
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", "/ISAPI/System/IO/inputs/1")
    assert body == INPUT_1.replace("<enabled>false<", "<enabled>true<")
    assert hass.states.get(armed.entity_id).state == STATE_ON

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_values_outlive_a_few_failed_reads(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    device.documents.update({"/ISAPI/System/status": STATUS, "/ISAPI/System/IO/inputs/1": INPUT_1})
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    armed = _entity(hass, entry, "switch", "_armed_1").entity_id
    assert hass.states.get(armed).state == STATE_OFF
    refresher = entry.runtime_data.refresher
    link = entry.runtime_data.link

    async def refresh() -> str:
        refresher.request_full()
        await refresher.async_refresh()
        await hass.async_block_till_done()
        return hass.states.get(armed).state

    # A busy device refuses a resource that answered before: not taken for unsupported.
    inputs = "/ISAPI/System/IO/inputs"
    device.overrides[inputs] = (
        403,
        "<ResponseStatus><statusCode>4</statusCode><subStatusCode>notSupport</subStatusCode></ResponseStatus>",
    )
    assert await refresh() == STATE_OFF
    assert await refresh() == STATE_OFF
    assert inputs not in link.unsupported
    assert await refresh() == STATE_UNAVAILABLE

    del device.overrides[inputs]
    assert await refresh() == STATE_OFF

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _video_inputs(res_desc: str) -> str:
    return (
        f"<VideoInputChannelList {NS}><VideoInputChannel><id>1</id><inputPort>1</inputPort>"
        f"<name>Camera 01</name><resDesc>{res_desc}</resDesc></VideoInputChannel>"
        "</VideoInputChannelList>"
    )


async def test_channel_unavailable_during_video_loss(hass: HomeAssistant, serve) -> None:
    device = FakeDevice("nvr")
    inputs = "/ISAPI/System/Video/inputs/channels"
    device.documents[inputs] = _video_inputs("1080P30")
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    refresher = entry.runtime_data.refresher
    lost = {
        _entity(hass, entry, "camera", "_ch1_stream_1").entity_id,
        _entity(hass, entry, "binary_sensor", "_ch1_event_motion").entity_id,
    }
    # Settings, connection and recording state do not need a picture.
    kept = {
        _entity(hass, entry, "binary_sensor", "_ch1_recording").entity_id,
        _entity(hass, entry, "binary_sensor", "_ch1_online").entity_id,
        _entity(hass, entry, "switch", "_ch1_switch_motion").entity_id,
        _entity(hass, entry, "binary_sensor", "_ch2_recording").entity_id,
        _entity(hass, entry, "sensor", "_network_address").entity_id,
    }
    video_loss = _entity(hass, entry, "binary_sensor", "_ch1_event_video_loss").entity_id

    async def refresh(res_desc: str) -> None:
        device.documents[inputs] = _video_inputs(res_desc)
        await refresher.async_refresh()
        await hass.async_block_till_done()

    def unavailable() -> set[str]:
        return {e for e in lost | kept if hass.states.get(e).state == STATE_UNAVAILABLE}

    assert unavailable() == set()
    assert hass.states.get(video_loss).state == STATE_OFF

    await refresh("NO VIDEO")
    assert unavailable() == lost
    assert hass.states.get(video_loss).state == STATE_ON

    await refresh("1080P30")
    assert unavailable() == set()
    assert hass.states.get(video_loss).state == STATE_OFF

    # A list that stops answering does not keep the channel hidden for good.
    await refresh("NO VIDEO")
    device.overrides[inputs] = (500, "<ResponseStatus><statusCode>6</statusCode></ResponseStatus>")
    for _ in range(3):
        assert unavailable() == lost
        await refresher.async_refresh()
        await hass.async_block_till_done()
    assert unavailable() == set()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
