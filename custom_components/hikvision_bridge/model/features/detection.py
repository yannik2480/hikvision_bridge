"""Enable/disable state of per-channel detections and whether an area is drawn."""

from __future__ import annotations

from dataclasses import dataclass
import re

from ...wire.faults import DeviceRejected, Unsupported
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
# Switched with a document that holds nothing but ``enabled``: some firmware redraws the
# detection area as a full-frame rectangle whenever the layout is written back, even unchanged.
_ENABLED_ONLY: frozenset[Kind] = frozenset({Kind.MOTION})
_ROOT_TAG = re.compile(r"<([A-Za-z_][\w.-]*)(?:\s[^<>]*)?>")
_BETWEEN_TAGS = re.compile(r">\s+<")


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
    if kind in _ENABLED_ONLY:
        document = await _switch_enabled_only(link, path, value)
    else:
        document = await patch_and_verify(link, path, {"enabled": value})
    return interpret(kind, parse(document.text))


def _same(left: str, right: str) -> bool:
    return _BETWEEN_TAGS.sub("><", left.strip()) == _BETWEEN_TAGS.sub("><", right.strip())


async def _switch_enabled_only(link: IsapiLink, path: str, value: str) -> RawDocument:
    """Send only ``enabled``; write the complete document when that does not do the job.

    The complete document follows when the device refuses the short one, ignores it or
    changes anything besides ``enabled``, which also puts those other values back.
    """
    wanted = RawDocument((await link.call("GET", path)).text)
    root = _ROOT_TAG.search(wanted.text)
    if root is None or not wanted.set("enabled", value):
        raise Unsupported(f"{path} has no element enabled")
    short = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"{root.group(0)}\n<enabled>{value}</enabled>\n</{root.group(1)}>\n"
    )
    try:
        await link.write_xml(path, short)
    except (DeviceRejected, Unsupported):
        pass
    else:
        after = (await link.call("GET", path)).text
        if _same(after, wanted.text):
            return wanted

    await link.write_xml(path, wanted.text)
    actual = RawDocument((await link.call("GET", path)).text).get("enabled")
    if actual is not None and actual.lower() != value:
        raise VerifyMismatch(
            f"enabled reads back as {actual!r} instead of {value!r}", 200, "verify"
        )
    return wanted
