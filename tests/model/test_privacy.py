"""Privacy mask: when it counts as covering the frame, and what a write puts in the document."""

from __future__ import annotations

from custom_components.hikvision_bridge.model.features import privacy
from custom_components.hikvision_bridge.wire.xmlview import RawDocument, parse


def _mask(points: list[tuple[int, int]], *, enabled: bool = False, size=(704, 576)) -> str:
    coords = "".join(
        f"<RegionCoordinates><positionX>{x}</positionX><positionY>{y}</positionY></RegionCoordinates>"
        for x, y in points
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<PrivacyMask version="2.0" xmlns="http://www.std-cgi.com/ver20/XMLSchema">'
        f"<enabled>{str(enabled).lower()}</enabled>"
        f"<normalizedScreenSize><normalizedScreenWidth>{size[0]}</normalizedScreenWidth>"
        f"<normalizedScreenHeight>{size[1]}</normalizedScreenHeight></normalizedScreenSize>"
        '<PrivacyMaskRegionList size="1"><PrivacyMaskRegion version="2.0">'
        f"<id>1</id><enabled>{str(enabled).lower()}</enabled>"
        f"<RegionCoordinatesList>{coords}</RegionCoordinatesList>"
        "</PrivacyMaskRegion></PrivacyMaskRegionList></PrivacyMask>"
    )


FULL = [(0, 0), (704, 0), (704, 576), (0, 576)]


def test_a_mask_over_the_whole_frame_is_full_frame() -> None:
    assert privacy.interpret(parse(_mask(FULL))) == privacy.PrivacyState(False, True)
    assert privacy.interpret(parse(_mask(FULL, enabled=True))).enabled is True


def test_edges_the_firmware_moved_a_little_still_count() -> None:
    drifted = [(0, 5), (704, 5), (704, 576), (0, 576)]
    assert privacy.interpret(parse(_mask(drifted))).full_frame is True


def test_a_partial_mask_is_not_full_frame() -> None:
    left_quarter = [(0, 0), (176, 0), (176, 576), (0, 576)]
    middle_band = [(0, 200), (704, 200), (704, 380), (0, 380)]
    assert privacy.interpret(parse(_mask(left_quarter))).full_frame is False
    assert privacy.interpret(parse(_mask(middle_band))).full_frame is False
    assert privacy.interpret(parse(_mask([(0, 0), (704, 0)]))).full_frame is False  # no area


def test_a_document_without_a_screen_size_is_not_full_frame() -> None:
    text = _mask(FULL).replace("normalizedScreenWidth", "x").replace("normalizedScreenHeight", "y")
    assert privacy.interpret(parse(text)).full_frame is False


def test_snapping_writes_the_frame_corners_whatever_the_point_order() -> None:
    shifted = [(0, 4), (704, 4), (704, 576), (0, 576)]
    raw = RawDocument(_mask(shifted))
    parsed = parse(raw.text)
    privacy._snap_to_frame(raw, parsed, 0, on=True)
    assert privacy._regions(parse(raw.text)) == [FULL]
    # a second pass changes nothing: the document is the same on every write
    again = RawDocument(raw.text)
    privacy._snap_to_frame(again, parse(again.text), 0, on=True)
    assert again.text == raw.text


def test_snapping_only_touches_the_chosen_region() -> None:
    text = _mask(FULL).replace(
        "</PrivacyMaskRegionList>",
        "<PrivacyMaskRegion><id>2</id><enabled>true</enabled><RegionCoordinatesList>"
        "<RegionCoordinates><positionX>10</positionX><positionY>20</positionY></RegionCoordinates>"
        "<RegionCoordinates><positionX>50</positionX><positionY>20</positionY></RegionCoordinates>"
        "<RegionCoordinates><positionX>50</positionX><positionY>60</positionY></RegionCoordinates>"
        "</RegionCoordinatesList></PrivacyMaskRegion></PrivacyMaskRegionList>",
    )
    raw = RawDocument(text)
    privacy._snap_to_frame(raw, parse(raw.text), 0, on=False)
    regions = privacy._regions(parse(raw.text))
    assert regions[0] == FULL
    assert regions[1] == [(10, 20), (50, 20), (50, 60)]
