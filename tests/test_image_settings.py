"""Image settings become entities per channel; channel devices follow their camera link."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hikvision_bridge.const import DOMAIN, SUBENTRY_CHANNEL_LINK
from custom_components.hikvision_bridge.model.features.image import (
    option_key,
    parse_capabilities,
    parse_values,
)
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.conftest import device_entry
from tests.fakedevice import FakeDevice

NS = 'version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"'
BASE = "/ISAPI/Image/channels/1"

CAPS = f"""<?xml version="1.0" encoding="UTF-8"?>
<ImageChannel {NS}>
<id>1</id>
<IrcutFilter>
<IrcutFilterType opt="day,night,auto">auto</IrcutFilterType>
<nightToDayFilterLevel opt="0,1,2,3">2</nightToDayFilterLevel>
</IrcutFilter>
<Color>
<brightnessLevel min="0" max="100">50</brightnessLevel>
</Color>
<Exposure>
<ExposureType opt="auto">auto</ExposureType>
</Exposure>
<HLC>
<enabled opt="true,false">false</enabled>
</HLC>
<SupplementLight>
<supplementLightMode opt="irLight,colorVuWhiteLight,close">irLight</supplementLightMode>
<irLightBrightness min="0" max="100">50</irLightBrightness>
<whiteLightBrightness min="0" max="100">50</whiteLightBrightness>
</SupplementLight>
</ImageChannel>
"""

COLOR = f"""<?xml version="1.0" encoding="UTF-8"?>
<Color {NS}>
<brightnessLevel>40</brightnessLevel>
<contrastLevel>45</contrastLevel>
</Color>
"""

LIGHT = f"""<?xml version="1.0" encoding="UTF-8"?>
<SupplementLight {NS}>
<supplementLightMode>irLight</supplementLightMode>
<irLightBrightness>30</irLightBrightness>
</SupplementLight>
"""

IRCUT = f"""<?xml version="1.0" encoding="UTF-8"?>
<IrcutFilter {NS}>
<IrcutFilterType>auto</IrcutFilterType>
<nightToDayFilterLevel>2</nightToDayFilterLevel>
</IrcutFilter>
"""

IMAGE = f"""<?xml version="1.0" encoding="UTF-8"?>
<ImageChannel {NS}>
<id>1</id>
<IrcutFilter {NS}>
<IrcutFilterType>auto</IrcutFilterType>
<nightToDayFilterLevel>2</nightToDayFilterLevel>
</IrcutFilter>
<Exposure {NS}>
<ExposureType>auto</ExposureType>
</Exposure>
<HLC {NS}>
<enabled>false</enabled>
</HLC>
{COLOR.split("?>", 1)[1]}
{LIGHT.split("?>", 1)[1]}
</ImageChannel>
"""


def _image_device(**kwargs) -> FakeDevice:
    device = FakeDevice("nvr", **kwargs)
    device.documents.update(
        {
            f"{BASE}/capabilities": CAPS,
            BASE: IMAGE,
            f"{BASE}/color": COLOR,
            f"{BASE}/supplementLight": LIGHT,
            f"{BASE}/IrcutFilter": IRCUT,
        }
    )
    return device


def _entity_id(hass: HomeAssistant, entry, domain: str, suffix: str) -> str:
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if item.domain == domain and item.unique_id.endswith(suffix):
            return item.entity_id
    raise AssertionError(f"no {domain} entity ending in {suffix}")


def test_capabilities_skip_fixed_settings() -> None:
    found = parse_capabilities(parse(CAPS))
    assert found["day_night"].options == ("day", "night", "auto")
    assert (found["day_night_sensitivity"].minimum, found["day_night_sensitivity"].maximum) == (
        0,
        3,
    )
    assert (found["brightness"].minimum, found["brightness"].maximum) == (0, 100)
    assert "hlc" in found
    assert "exposure_mode" not in found  # a single option cannot be changed


def test_values_follow_the_active_mode() -> None:
    values = parse_values(parse(IMAGE))
    assert values["ir_light_brightness"].field == "irLightBrightness"
    assert values["ir_light_brightness"].value == "30"
    assert "white_light_brightness" not in values
    assert option_key("colorVuWhiteLight") == "colorvuwhitelight"
    assert option_key("1/25") == "1/25"


async def test_image_settings_read_and_write_one_section(hass: HomeAssistant, serve) -> None:
    device = _image_device()
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    day_night = hass.states.get(_entity_id(hass, entry, "select", "_ch1_image_day_night"))
    assert day_night.state == "auto"
    assert day_night.attributes["options"] == ["day", "night", "auto"]
    brightness = _entity_id(hass, entry, "number", "_ch1_image_brightness")
    assert float(hass.states.get(brightness).state) == 40
    ir = _entity_id(hass, entry, "number", "_ch1_image_ir_light_brightness")
    assert float(hass.states.get(ir).state) == 30
    # white light is not part of the active IR mode
    white = _entity_id(hass, entry, "number", "_ch1_image_white_light_brightness")
    assert hass.states.get(white).state == STATE_UNAVAILABLE

    device.writes.clear()
    await hass.services.async_call(
        "number", "set_value", {"entity_id": brightness, "value": 70}, blocking=True
    )
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", f"{BASE}/color")
    assert body == COLOR.replace("<brightnessLevel>40<", "<brightnessLevel>70<")

    device.writes.clear()
    select = _entity_id(hass, entry, "select", "_ch1_image_supplement_light")
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "colorvuwhitelight"},
        blocking=True,
    )
    method, path, body = device.writes[0]
    assert path == f"{BASE}/supplementLight"
    assert "<supplementLightMode>colorVuWhiteLight</supplementLightMode>" in body

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_linked_channel_device_belongs_to_its_subentry(hass: HomeAssistant, serve) -> None:
    recorder = FakeDevice("nvr")
    recorder_server = await serve(recorder)
    entry = device_entry(recorder_server, recorder)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id="testserial101")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    camera = FakeDevice("camera_g3", password="camera-own-pass")
    server = await serve(camera)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "device"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            "connection": "http",
            "verify_tls": True,
            CONF_USERNAME: camera.username,
            CONF_PASSWORD: camera.password,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "link_to_channel"}
    )
    assert result["reason"] == "linked_to_channel"
    await hass.async_block_till_done()  # the entry reloads with the new link

    (link,) = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CHANNEL_LINK]
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    linked = [d for d in devices if d.config_subentry_id == link.subentry_id]
    assert len(linked) == 1
    others = [d for d in devices if d is not linked[0]]
    assert others
    assert all(d.config_subentry_id is None for d in others)

    entities = er.async_entries_for_device(er.async_get(hass), linked[0].id)
    assert entities
    assert {e.config_subentry_id for e in entities} == {link.subentry_id}

    # Without the link the channel device is back with the recorder.
    identifier = next(iter(linked[0].identifiers))
    hass.config_entries.async_remove_subentry(entry, link.subentry_id)
    await hass.async_block_till_done()
    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None
    assert device.config_subentry_id is None
    entities = er.async_entries_for_device(er.async_get(hass), device.id)
    assert entities
    assert {e.config_subentry_id for e in entities} == {None}

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_white_light_switches_the_supplement_light(hass: HomeAssistant, serve) -> None:
    device = _image_device()
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    light = _entity_id(hass, entry, "light", "_ch1_white_light")
    state = hass.states.get(light)
    assert state.state == "off"
    assert state.attributes["supported_color_modes"] == ["brightness"]

    device.writes.clear()
    await hass.services.async_call("light", "turn_on", {"entity_id": light}, blocking=True)
    # The camera only lights the lamp at night: night mode first, then the white light.
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", f"{BASE}/IrcutFilter")
    assert "<IrcutFilterType>night</IrcutFilterType>" in body
    method, path, body = device.writes[1]
    assert (method, path) == ("PUT", f"{BASE}/supplementLight")
    assert "<supplementLightMode>colorVuWhiteLight</supplementLightMode>" in body
    assert "<irLightBrightness>30</irLightBrightness>" in body

    # The camera now reports night and white light in its image document too.
    device.documents[BASE] = (
        IMAGE.replace(">auto</IrcutFilterType>", ">night</IrcutFilterType>")
        .replace(">irLight<", ">colorVuWhiteLight<")
        .replace("<irLightBrightness>30", "<whiteLightBrightness>30")
        .replace("30</irLightBrightness>", "30</whiteLightBrightness>")
    )
    refresher = entry.runtime_data.refresher
    refresher.request_full()
    await refresher.async_refresh()
    assert hass.states.get(light).state == "on"

    device.writes.clear()
    await hass.services.async_call("light", "turn_off", {"entity_id": light}, blocking=True)
    bodies = [body for _, _, body in device.writes]
    assert any("<supplementLightMode>irLight</supplementLightMode>" in b for b in bodies)
    assert any("<IrcutFilterType>auto</IrcutFilterType>" in b for b in bodies)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_white_light_off_goes_back_to_another_mode(hass: HomeAssistant, serve) -> None:
    white = LIGHT.replace(">irLight<", ">colorVuWhiteLight<").replace(
        "<irLightBrightness>30</irLightBrightness>",
        "<whiteLightBrightness>40</whiteLightBrightness>",
    )
    device = _image_device()
    device.documents[f"{BASE}/supplementLight"] = white
    device.documents[BASE] = IMAGE.replace(LIGHT.split("?>", 1)[1], white.split("?>", 1)[1])
    server = await serve(device)
    entry = device_entry(server, device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    light = _entity_id(hass, entry, "light", "_ch1_white_light")
    state = hass.states.get(light)
    assert state.state == "on"
    assert state.attributes["brightness"] == 102

    device.writes.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": light, "brightness": 255}, blocking=True
    )
    # Already white: night mode and the brightness only.
    assert [path for _, path, _ in device.writes] == [
        f"{BASE}/IrcutFilter",
        f"{BASE}/supplementLight",
    ]
    assert "<whiteLightBrightness>100</whiteLightBrightness>" in device.writes[1][2]

    device.writes.clear()
    await hass.services.async_call("light", "turn_off", {"entity_id": light}, blocking=True)
    body = device.writes[0][2]
    # No mode from before is known (it was on already): the IR light, the camera has no smart mode.
    assert "<supplementLightMode>irLight</supplementLightMode>" in body

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
