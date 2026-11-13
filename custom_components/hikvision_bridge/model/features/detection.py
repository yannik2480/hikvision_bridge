"""Enable/disable state of per-channel detections and whether an area is drawn."""

from __future__ import annotations

from dataclasses import dataclass

from ...wire.faults import BridgeFault, Unsupported
from ...wire.link import IsapiLink
from ...wire.xmlview import RawDocument, XmlNode, parse
from ..ids import seg
from ..vocabulary import Kind
from . import VerifyMismatch, patch_and_verify

# Where each switchable detection is configured, relative to /ISAPI, per channel.
_PATHS: dict[Kind, str] = {
    Kind.MOTION: "/ISAPI/System/Video/inputs/channels/{ch}/motionDetection",
    Kind.TAMPER: "/ISAPI/System/Video/inputs/channels/{ch}/tamperDetection",
    Kind.INTRUSION: "/ISAPI/Smart/FieldDetection/{ch}",
    Kind.LINE_CROSSING: "/ISAPI/Smart/LineDetection/{ch}",
    Kind.REGION_ENTRANCE: "/ISAPI/Smart/regionEntrance/{ch}",
    Kind.REGION_EXITING: "/ISAPI/Smart/regionExiting/{ch}",
    Kind.SCENE_CHANGE: "/ISAPI/Smart/SceneChangeDetection/{ch}",
    Kind.VIDEO_LOSS: "/ISAPI/System/Video/inputs/channels/{ch}/videoLoss",
}
# Watched by the recorder itself, not by the camera: always configured on the recorder.
RECORDER_ONLY: frozenset[Kind] = frozenset({Kind.VIDEO_LOSS})


@dataclass(frozen=True, slots=True)
class DetectionState:
    enabled: bool
    area_defined: bool | None
    target_filter: tuple[str, ...] = ()


def config_path(kind: Kind, channel_id: str) -> str | None:
    template = _PATHS.get(kind)
    return template.format(ch=seg(channel_id)) if template else None


def _has_points(nodes: list[XmlNode]) -> bool:
    for node in nodes:
        x = node.numeric("positionX", 0) or 0
        y = node.numeric("positionY", 0) or 0
        if x or y:
            return True
    return False


def interpret(kind: Kind, document: XmlNode) -> DetectionState:
    enabled = bool(document.flag("enabled", False))
    area: bool | None
    if kind == Kind.LINE_CROSSING:
        area = _has_points(list(document.walk("Coordinates")))
    elif kind in (Kind.INTRUSION, Kind.REGION_ENTRANCE, Kind.REGION_EXITING):
        area = _has_points(list(document.walk("RegionCoordinates")))
    elif kind == Kind.MOTION:
        area = None  # grid layouts are not decoded; the device default covers the frame
    else:
        area = None
    targets = document.text("MotionDetectionLayout/targetType") or ""
    return DetectionState(
        enabled=enabled,
        area_defined=area,
        target_filter=tuple(t.strip() for t in targets.split(",") if t.strip()),
    )


async def read(link: IsapiLink, kind: Kind, channel_id: str) -> DetectionState | None:
    path = config_path(kind, channel_id)
    if path is None:
        return None
    document = await link.read_xml(path, optional=True)
    return interpret(kind, document) if document is not None else None


async def switch(link: IsapiLink, kind: Kind, channel_id: str, on: bool) -> DetectionState:
    path = config_path(kind, channel_id)
    if path is None:
        raise ValueError(f"{kind} cannot be switched")
    value = "true" if on else "false"
    if kind == Kind.MOTION:
        document = await _switch_motion(link, path, value)
    else:
        document = await patch_and_verify(link, path, {"enabled": value})
    return interpret(kind, parse(document.text))


async def _draws_polygons(link: IsapiLink, path: str, document: XmlNode) -> bool:
    """Whether the motion area is a polygon that the device reports under ``regionType`` grid.

    Polygon firmware (G3, V5.11) answers ``grid`` but redraws the polygon as the bounding box
    of ``gridMap`` when that is written back. Its web UI writes ``region`` with an empty
    ``gridMap`` instead, which the capabilities offer as an option.
    """
    if (document.text("regionType") or "").lower() != "grid":
        return False
    if not _has_points(list(document.walk("RegionCoordinates"))):
        return False
    try:
        caps = await link.read_xml(f"{path}/capabilities", optional=True)
    except BridgeFault:
        return False
    node = caps.first("regionType") if caps is not None else None
    options = node.attrs.get("opt", "") if node is not None else ""
    return "region" in (option.strip().lower() for option in options.split(","))


async def _switch_motion(link: IsapiLink, path: str, value: str) -> RawDocument:
    """Write the motion document back the way the camera's web UI does, keeping the area."""
    document = RawDocument((await link.call("GET", path)).text)
    if not document.set("enabled", value):
        raise Unsupported(f"{path} has no element enabled")
    if await _draws_polygons(link, path, parse(document.text)):
        document.set("regionType", "region")
        grid = document.get("MotionDetectionLayout/layout/gridMap") or ""
        document.set("MotionDetectionLayout/layout/gridMap", "0" * len(grid))
    await link.write_xml(path, document.text)
    actual = RawDocument((await link.call("GET", path)).text).get("enabled")
    if actual is not None and actual.lower() != value:
        raise VerifyMismatch(
            f"enabled reads back as {actual!r} instead of {value!r}", 200, "verify"
        )
    return document
