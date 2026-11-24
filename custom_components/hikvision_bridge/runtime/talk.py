"""Talking from Home Assistant through a device's speaker (two-way audio).

The browser sends the microphone already encoded in the device's G.711 variant, 20 ms at a
time, and this passes it on unchanged: no decoding, no transcoding. One talk per device at a
time: a second one, from another dashboard or person, takes over and the first one ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum

from homeassistant.core import HomeAssistant

from ..const import DOMAIN
from ..model.features.talk import TalkChannel, session_id
from ..wire.faults import BridgeFault
from ..wire.link import IsapiLink, Upload
from ..wire.scrub import scrubbed_logger
from ..wire.xmlview import XmlError

_LOGGER = scrubbed_logger(__name__)
DATA_KEY = f"{DOMAIN}_talks"


class TalkEnd(StrEnum):
    """Why a talk ended, as the card is told."""

    STOPPED = "stopped"  # the person stopped it, or left the page
    REPLACED = "replaced"  # someone else started talking to the device
    DEVICE = "device"  # the device closed the connection
    UNLOADED = "unloaded"  # the integration entry was unloaded


class TalkError(Exception):
    """The talk could not start; ``reason`` is a translation key of the card."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Talk:
    """One open two-way audio session on a device."""

    def __init__(
        self, link: IsapiLink, channel: TalkChannel, on_end: Callable[[TalkEnd], None]
    ) -> None:
        self.link = link
        self.channel = channel
        self._on_end = on_end
        self._upload: Upload | None = None
        self._watch: asyncio.Task[None] | None = None
        self._ended = False
        self.sent = 0
        self.dropped = 0

    @property
    def codec(self) -> str:
        return self.channel.codec

    async def open(self) -> None:
        """Close what a previous session may have left open, open the channel, then the body."""
        path = self.channel.path
        with suppress(BridgeFault):
            await self.link.call("PUT", f"{path}/close")
        try:
            reply = await self.link.call("PUT", f"{path}/open")
            session = None
            if reply.body:
                with suppress(XmlError):
                    session = session_id(reply.xml())
            data_path = f"{path}/audioData" + (f"?sessionId={session}" if session else "")
            self._upload = await self.link.open_upload(data_path)
        except BridgeFault as err:
            _LOGGER.debug("Two-way audio did not open on %s: %r", self.link.endpoint.host, err)
            with suppress(BridgeFault):
                await self.link.call("PUT", f"{path}/close")
            raise TalkError("talk_failed") from err
        self._watch = asyncio.get_running_loop().create_task(self._watch_device())

    async def _watch_device(self) -> None:
        upload = self._upload
        if upload is None:
            return
        await upload.wait_closed_by_device()
        if not self._ended:
            _LOGGER.debug("%s ended the two-way audio", self.link.endpoint.host)
            await self.close(TalkEnd.DEVICE)

    def send(self, frame: bytes) -> None:
        """Pass a frame of the microphone on; frames the device cannot take yet are dropped."""
        if self._upload is None or self._ended:
            return
        if self._upload.write(frame):
            self.sent += len(frame)
        else:
            self.dropped += len(frame)

    async def close(self, reason: TalkEnd) -> None:
        """End the talk once: the socket, then the channel on the device."""
        if self._ended:
            return
        self._ended = True
        if self._upload is not None:
            self._upload.close()
        if self._watch is not None and self._watch is not asyncio.current_task():
            self._watch.cancel()
        with suppress(BridgeFault):
            await self.link.call("PUT", f"{self.channel.path}/close")
        _LOGGER.debug(
            "Two-way audio on %s ended (%s): %d bytes sent, %d dropped",
            self.link.endpoint.host,
            reason,
            self.sent,
            self.dropped,
        )
        self._on_end(reason)


class TalkManager:
    """The talk of each device, by config entry."""

    def __init__(self) -> None:
        self._talks: dict[str, Talk] = {}
        self._lock = asyncio.Lock()

    def active(self, entry_id: str) -> Talk | None:
        return self._talks.get(entry_id)

    async def start(
        self,
        entry_id: str,
        link: IsapiLink,
        channel: TalkChannel,
        on_end: Callable[[TalkEnd], None],
    ) -> Talk:
        """Open a talk on a device, ending the one already there."""
        async with self._lock:
            if previous := self._talks.pop(entry_id, None):
                await previous.close(TalkEnd.REPLACED)

            def ended(reason: TalkEnd) -> None:
                if self._talks.get(entry_id) is talk:
                    del self._talks[entry_id]
                on_end(reason)

            talk = Talk(link, channel, ended)
            await talk.open()
            self._talks[entry_id] = talk
            return talk

    async def stop_entry(self, entry_id: str) -> None:
        if talk := self._talks.get(entry_id):
            await talk.close(TalkEnd.UNLOADED)


def manager(hass: HomeAssistant) -> TalkManager:
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = TalkManager()
    return hass.data[DATA_KEY]
