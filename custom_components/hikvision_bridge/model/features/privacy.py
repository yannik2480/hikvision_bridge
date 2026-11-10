"""Privacy mask: hide the whole picture of a camera on demand.

The switch is meant for a mask that covers the full frame (a "privacy mode"). Drawing areas is
done on the device, and a camera whose mask covers only part of the picture gets no switch, so a
drawn mask is never replaced.

Some firmware (found on the VK383F cameras) moves the top edge of a region down by one pixel on
every write of the mask, even when the document is sent back unchanged, so a plain read, flip the
flag, write cycle slowly shrinks the mask. Writing the corners of the frame itself avoids this: a
full-frame mask is the same document on every write, and 0 is the correct value for the top edge
anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...wire.faults import Unsupported
from ...wire.link import IsapiLink
from ...wire.xmlview import RawDocument, XmlNode, parse
from ..ids import seg
from . import VerifyMismatch

PATH = "/ISAPI/System/Video/inputs/channels/{ch}/privacyMask"
_REGION = "PrivacyMaskRegionList/PrivacyMaskRegion"
_POINT = f"{_REGION}/RegionCoordinatesList/RegionCoordinates"
# A region this close to the frame edges (in pixels of the mask's own grid, or 2 %, whichever
# is larger) counts as covering the frame; the firmware quirk above moves edges a little.
_MIN_MARGIN = 8
_MARGIN_SHARE = 0.02


@dataclass(frozen=True, slots=True)
class PrivacyState:
    enabled: bool
    full_frame: bool


def config_path(channel_id: str) -> str:
    return PATH.format(ch=seg(channel_id))


def _screen(document: XmlNode) -> tuple[int, int] | None:
    width = document.numeric("normalizedScreenSize/normalizedScreenWidth")
    height = document.numeric("normalizedScreenSize/normalizedScreenHeight")
    return (width, height) if width and height else None


def _regions(document: XmlNode) -> list[list[tuple[int, int]]]:
    """The corner points of every region, in document order."""
    return [
        [
            (point.numeric("positionX", 0) or 0, point.numeric("positionY", 0) or 0)
            for point in region.walk("RegionCoordinates")
        ]
        for region in document.walk("PrivacyMaskRegion")
    ]


def _covers_frame(points: list[tuple[int, int]], width: int, height: int) -> bool:
    if len(points) < 3:
        return False
    margin_x = max(_MIN_MARGIN, round(width * _MARGIN_SHARE))
    margin_y = max(_MIN_MARGIN, round(height * _MARGIN_SHARE))
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (
        min(xs) <= margin_x
        and min(ys) <= margin_y
        and max(xs) >= width - margin_x
        and max(ys) >= height - margin_y
    )


def _full_frame_region(document: XmlNode) -> int | None:
    """Index of the first region that covers the whole frame."""
    screen = _screen(document)
    if screen is None:
        return None
    for index, points in enumerate(_regions(document)):
        if _covers_frame(points, *screen):
            return index
    return None


def interpret(document: XmlNode) -> PrivacyState:
    return PrivacyState(
        enabled=bool(document.flag("enabled", False)),
        full_frame=_full_frame_region(document) is not None,
    )


async def read(link: IsapiLink, channel_id: str) -> PrivacyState | None:
    """The mask's state, or None if the channel has no privacy mask."""
    document = await link.read_xml(config_path(channel_id), optional=True)
    return interpret(document) if document is not None else None


def _snap_to_frame(raw: RawDocument, parsed: XmlNode, index: int, on: bool) -> None:
    """Write the corners of the frame into one region (each point to its nearest corner)."""
    width, height = _screen(parsed) or (0, 0)
    first = sum(len(points) for points in _regions(parsed)[:index])
    for offset, (x, y) in enumerate(_regions(parsed)[index]):
        raw.set(f"{_POINT}/positionX", str(width if x * 2 > width else 0), first + offset)
        raw.set(f"{_POINT}/positionY", str(height if y * 2 > height else 0), first + offset)
    if on:
        raw.set(f"{_REGION}/enabled", "true", index)


async def switch(link: IsapiLink, channel_id: str, on: bool) -> PrivacyState:
    """Turn the mask on or off: its own flag, and the frame corners of its full-frame region."""
    path = config_path(channel_id)
    raw = RawDocument((await link.call("GET", path)).text)
    parsed = parse(raw.text)
    index = _full_frame_region(parsed)
    if index is None:
        raise Unsupported(f"{path} has no mask covering the whole frame")
    if not raw.set("enabled", "true" if on else "false"):
        raise Unsupported(f"{path} has no element enabled")
    _snap_to_frame(raw, parsed, index, on)
    await link.write_xml(path, raw.text)
    state = interpret(parse((await link.call("GET", path)).text))
    if state.enabled != on:
        raise VerifyMismatch(
            f"enabled reads back as {state.enabled} instead of {on}", 200, "verify"
        )
    return state
