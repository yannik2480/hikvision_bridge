"""Two-way audio: the channel a device plays sound from Home Assistant on, and how to open it.

The device lists its channels at /ISAPI/System/TwoWayAudio/channels, each with the codec it
expects. Talking is close (a previous session may still hold the channel), open, then one
PUT of audioData whose body is the sound itself, raw G.711 for as long as the talk lasts.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...wire.link import IsapiLink
from ...wire.xmlview import XmlNode
from ..ids import seg

PATH = "/ISAPI/System/TwoWayAudio/channels"
# The codecs the browser encodes; devices offering only others (G.726, AAC) get no microphone.
CODECS = ("G.711ulaw", "G.711alaw")


@dataclass(frozen=True, slots=True)
class TalkChannel:
    """A two-way audio channel and the G.711 variant it expects."""

    channel_id: str
    codec: str

    @property
    def path(self) -> str:
        return f"{PATH}/{seg(self.channel_id)}"


def parse_channels(document: XmlNode | None) -> TalkChannel | None:
    """The first enabled channel with a G.711 codec."""
    if document is None:
        return None
    for node in document.children("TwoWayAudioChannel"):
        channel_id = node.text("id")
        codec = node.text("audioCompressionType")
        if channel_id and codec in CODECS and node.flag("enabled", True):
            return TalkChannel(channel_id, codec)
    return None


async def read(link: IsapiLink) -> TalkChannel | None:
    return parse_channels(await link.read_xml(PATH, optional=True))


def session_id(document: XmlNode | None) -> str | None:
    """The session some firmwares hand out on open; audioData then names it."""
    return document.text("sessionId") if document is not None else None
