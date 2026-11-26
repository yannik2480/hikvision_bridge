"""PTZ capabilities and presets."""

from __future__ import annotations

from aiohikvision.isapi.model.features import park, ptz
from aiohikvision.isapi.model.features.image import ImageValue
from aiohikvision.isapi.wire.xmlview import parse

NS = 'version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"'
CAPS = (
    f"<PTZChanelCap {NS}><ContinuousPanTiltSpace><XRange><Min>-100</Min><Max>100</Max></XRange>"
    "</ContinuousPanTiltSpace><ContinuousZoomSpace><XRange><Min>-100</Min><Max>100</Max>"
    "</XRange></ContinuousZoomSpace><maxPresetNum>300</maxPresetNum></PTZChanelCap>"
)
PRESETS = (
    f"<PTZPresetList {NS}>"
    "<PTZPreset><enabled>true</enabled><id>1</id><presetName>Gate</presetName></PTZPreset>"
    "<PTZPreset><enabled>true</enabled><id>2</id><presetName>Gate</presetName></PTZPreset>"
    "<PTZPreset><enabled>true</enabled><id>3</id><presetName/></PTZPreset>"
    "<PTZPreset><enabled>false</enabled><id>4</id><presetName>Unset</presetName></PTZPreset>"
    "</PTZPresetList>"
)


def test_ptz_capabilities() -> None:
    found = ptz.parse_capabilities(parse(CAPS), parse(PRESETS))
    assert found is not None
    assert found.directions == list(ptz.DIRECTIONS)
    assert found.presets == {"1": "Gate", "2": "Gate (2)", "3": "Preset 3"}


def test_zoom_only_lens() -> None:
    caps = parse(
        f"<PTZChanelCap {NS}><ContinuousZoomSpace><XRange><Min>-100</Min><Max>100</Max>"
        "</XRange></ContinuousZoomSpace></PTZChanelCap>"
    )
    found = ptz.parse_capabilities(caps, None)
    assert found is not None
    assert found.directions == ["zoom_in", "zoom_out"]


def test_no_ptz() -> None:
    assert ptz.parse_capabilities(None, None) is None
    assert ptz.parse_capabilities(parse(f"<PTZChanelCap {NS}/>"), None) is None


def test_switched_off_serial_ptz() -> None:
    """An NVR offers RS-485 PTZ for a fixed camera, switched off."""
    caps = parse(
        f'<PTZChannelCap {NS}><id min="1" max="8">2</id><enabled opt="true,false">false</enabled>'
        '<controlProtocol opt="PELCO-P,PELCO-D">HIKVISION</controlProtocol>'
        "<ContinuousPanTiltSpace><XRange><Min>-100</Min><Max>100</Max></XRange>"
        "</ContinuousPanTiltSpace></PTZChannelCap>"
    )
    assert ptz.parse_capabilities(caps, None) is None


def test_fixed_lens_without_pan_and_tilt() -> None:
    """A fixed camera describes serial PTZ; its settings deny pan and tilt."""
    fixed = parse(
        f"<PTZChannel {NS}><id>1</id><enabled>true</enabled><controlProtocol>PELCO-D"
        "</controlProtocol><panSupport>false</panSupport><tiltSupport>false</tiltSupport>"
        "<zoomSupport>true</zoomSupport></PTZChannel>"
    )
    # Its zoom is the digital one; serial commands move nothing.
    assert ptz.parse_capabilities(parse(CAPS), None, fixed) is None
    motor_zoom = parse(
        f"<PTZChannel {NS}><controlProtocol>HIKVISION</controlProtocol>"
        "<panSupport>false</panSupport><tiltSupport>false</tiltSupport>"
        "<zoomSupport>true</zoomSupport></PTZChannel>"
    )
    found = ptz.parse_capabilities(parse(CAPS), None, motor_zoom)
    assert found is not None
    assert found.directions == ["zoom_in", "zoom_out"]
    no_zoom = parse(
        f"<PTZChannel {NS}><panSupport>false</panSupport><tiltSupport>false</tiltSupport>"
        "<zoomSupport>false</zoomSupport></PTZChannel>"
    )
    assert ptz.parse_capabilities(parse(CAPS), None, no_zoom) is None
    switched_off = parse(f"<PTZChannel {NS}><enabled>false</enabled></PTZChannel>")
    assert ptz.parse_capabilities(parse(CAPS), None, switched_off) is None


PARK = (
    f"<ParkAction {NS}><enabled>false</enabled><Parktime>30</Parktime>"
    "<Action><ActionType>preset</ActionType><ActionNum>1</ActionNum></Action></ParkAction>"
)
PARK_CAPS = (
    f'<ParkAction {NS}><enabled opt="true,false"/><Parktime min="5" max="300"/>'
    '<Action><ActionType opt="patrol,pattern,preset"/><ActionNum min="1" max="8"/></Action>'
    "</ParkAction>"
)


def test_park_action() -> None:
    found = park.parse_capabilities(parse(PARK), parse(PARK_CAPS))
    assert set(found) == {"park_action", "park_time", "park_action_type", "park_action_number"}
    assert (found["park_time"].minimum, found["park_time"].maximum) == (5, 300)
    assert found["park_action_type"].options == ("patrol", "pattern", "preset")
    assert found["park_action_number"].maximum == 8
    values = park.parse_values(parse(PARK))
    assert values["park_action_type"] == ImageValue("Action/ActionType", "preset")
    assert values["park_time"].value == "30"


def test_park_action_without_capabilities() -> None:
    found = park.parse_capabilities(parse(PARK), None)
    assert (found["park_time"].minimum, found["park_time"].maximum) == park.DEFAULT_TIME
    assert found["park_action_type"].options == park.DEFAULT_TYPES
    assert park.parse_capabilities(None, parse(PARK_CAPS)) == {}


def test_park_action_numbers() -> None:
    presets = {"1": "Gate", "3": "Preset 3"}
    assert park.action_numbers("preset", park.DEFAULT_NUMBER, presets) == presets
    assert list(park.action_numbers("preset", park.DEFAULT_NUMBER, {})) == [
        str(n) for n in range(1, 33)
    ]
    assert list(park.action_numbers("patrol", park.DEFAULT_NUMBER, presets)) == [
        str(n) for n in range(1, 9)
    ]
    assert list(park.action_numbers("Pattern", park.DEFAULT_NUMBER, {})) == ["1", "2", "3", "4"]
    # The device's range narrows the web interface numbers.
    assert list(park.action_numbers("preset", (1, 8), {})) == [str(n) for n in range(1, 9)]
    assert park.action_numbers("autoscan", park.DEFAULT_NUMBER, presets) == {}
    assert park.action_numbers(None, park.DEFAULT_NUMBER, presets) == {}
