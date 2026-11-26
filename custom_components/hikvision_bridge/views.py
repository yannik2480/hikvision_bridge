"""HTTP views streaming recordings and their thumbnails from the device to the browser."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict
import math
import re
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PLAYBACK_H264, PLAYBACK_MP4, PLAYBACK_SEEKABLE
from .model.features import recordings
from .runtime import playback as playback_module, sessions, spool, vod
from .runtime.bridge import DeviceBridge
from .runtime.playback import (
    EXTENSION,
    FORMAT_ORIGINAL,
    FORMATS,
    MIME,
    NoRecording,
    PlaybackSource,
    RecordingEncrypted,
    ffmpeg_args,
    open_recording,
    pick_source,
    run_ffmpeg,
    thumbnail_args,
)
from .wire.faults import BridgeFault, detail
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

RECORDING_URL = "/api/hikvision_bridge/recording/{entry_id}/{channel_id}/{source}/{fmt}/{token}"
THUMBNAIL_URL = "/api/hikvision_bridge/thumbnail/{entry_id}/{channel_id}/{source}/{token}"
_PLAYBACK_URI = re.compile(r"^rtsp://[^/\s]+/Streaming/tracks/\d+/?\?[^\s<>]*$", re.IGNORECASE)
THUMBNAIL_CACHE = 256
# Formats streamed as fragmented MP4, whose header gets the length (see DurationStamp).
MP4_FORMATS = (PLAYBACK_MP4, PLAYBACK_H264)
# Pictures are small (about 320 pixels wide), but the cache stays bounded in bytes as well.
THUMBNAIL_CACHE_BYTES = 8 * 1024 * 1024
# Each thumbnail opens a download; recorders serve only a few at a time.
THUMBNAIL_PARALLEL = 2
# A download the device does not finish in this time gives up (and is not cached).
THUMBNAIL_TIMEOUT = 30
HLS = "application/vnd.apple.mpegurl"
_VOD_PART = re.compile(r"^([0-9]{1,7})\.m4s$")


def encode_token(playback_uri: str) -> str:
    return base64.urlsafe_b64encode(playback_uri.encode()).decode().rstrip("=")


def decode_token(token: str) -> str | None:
    """The playback URI of a token, or None unless it is one of a device's recordings."""
    try:
        uri = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return uri if _PLAYBACK_URI.match(uri) else None


def recording_url(
    entry_id: str,
    channel_id: str,
    source: str,
    fmt: str,
    playback_uri: str,
) -> str:
    return RECORDING_URL.format(
        entry_id=entry_id,
        channel_id=channel_id,
        source=source,
        fmt=fmt,
        token=encode_token(playback_uri),
    )


def thumbnail_url(entry_id: str, channel_id: str, source: str, playback_uri: str) -> str:
    return THUMBNAIL_URL.format(
        entry_id=entry_id,
        channel_id=channel_id,
        source=source,
        token=encode_token(playback_uri),
    )


def _find_source(
    hass: HomeAssistant, entry_id: str, channel_id: str, source: str
) -> tuple[DeviceBridge, PlaybackSource] | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    bridge = getattr(entry, "runtime_data", None) if entry and entry.domain == DOMAIN else None
    if not isinstance(bridge, DeviceBridge) or channel_id not in bridge.profile.channels:
        return None
    found = pick_source(bridge, channel_id, source)
    return (bridge, found) if found else None


def _seconds(value: str | None) -> float | None:
    try:
        number = float(value) if value else None
    except ValueError:
        return None
    return number if number and number > 0 else None


async def _length(
    playback: PlaybackSource, uri: str, offset: float | None, duration: float | None
) -> float | None:
    """Seconds of video a recording request gives: the file from ``offset``, or ``duration``."""
    if duration:
        return duration
    query = dict(parse_qsl(urlsplit(uri).query))
    try:
        wall = await recordings.wall_offset(playback.link)
    except BridgeFault:
        return None
    start = recordings.parse_time(query.get("starttime"), wall)
    end = recordings.parse_time(query.get("endtime"), wall)
    if start is None or end is None:
        return None
    # A file still recording is listed up to the end of the search.
    seconds = (min(end, dt_util.utcnow()) - start).total_seconds() - (offset or 0.0)
    return seconds if seconds > 0 else None


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
# Bytes read from a spool and sent at a time.
SPOOL_READ = 256 * 1024


def _byte_range(header: str | None) -> tuple[int | None, int | None] | None:
    """The one byte range asked for: (first, last), or (None, n) for the last n bytes.

    None for no range, or one this does not answer (several ranges, or last before first).
    """
    match = _RANGE.match((header or "").strip())
    if match is None or not (match[1] or match[2]):
        return None
    if not match[1]:
        return (None, int(match[2]))
    first = int(match[1])
    last = int(match[2]) if match[2] else None
    if last is not None and last < first:
        return None
    return (first, last)


async def _spool_reply(
    request: web.Request, found: spool.Spool, headers: dict[str, str]
) -> web.StreamResponse:
    """Answers from a spool: a byte range (206), or all of it (200).

    A range is answered with the bytes made so far if its end is not made yet; the player asks
    for the rest. Bytes not made yet are waited for, so a seek ahead of the conversion waits.
    The length is known (Content-Length) once the conversion is finished.
    """
    span = _byte_range(request.headers.get("Range"))
    with found.reading():
        if span is None:
            first, last = 0, None
        elif span[0] is None:
            await found.wait_for(math.inf)
            first, last = max(0, found.written - span[1]), found.written - 1
        else:
            first, last = span
            await found.wait_for(first)
            if last is not None:
                await found.wait_for(last)
        if found.error is not None and found.written <= first:
            raise found.error
        if span is not None and first >= found.written:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{found.written}"})
        total = found.written if found.finished else None
        if span is None:
            end = None
        elif last is None:
            end = found.written - 1
        else:
            end = min(last, found.written - 1)
        response = web.StreamResponse(
            status=200 if span is None else 206,
            headers={**headers, "Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
        )
        if span is not None:
            response.headers["Content-Range"] = (
                f"bytes {first}-{end}/{'*' if total is None else total}"
            )
            response.content_length = end - first + 1
        elif total is not None:
            response.content_length = total
        position = first
        try:
            await response.prepare(request)
            while end is None or position <= end:
                await found.wait_for(position)
                if found.written <= position:
                    break
                stop = found.written - 1 if end is None else min(end, found.written - 1)
                data = await found.read(position, min(SPOOL_READ, stop - position + 1))
                if not data:
                    break
                await response.write(data)
                position += len(data)
        except ConnectionResetError:
            # The player went away (closed, or seeking with a new request).
            return response
        incomplete = (end is not None and position <= end) or (
            end is None and found.error is not None
        )
        if incomplete:
            # Cut short: the player must not take part of it for the whole.
            _abort(request)
        else:
            await response.write_eof()
        return response


def _converter(
    hass: HomeAssistant,
    playback: PlaybackSource,
    *,
    uri: str,
    fmt: str,
    offset: float | None,
    duration: float | None,
    length: float | None,
) -> spool.Produce:
    """Makes the streamed MP4 of a recording into what it is given (see spool)."""

    async def produce(write: spool.Write) -> None:
        stamp = playback_module.DurationStamp(length) if length else None

        async def send(data: bytes) -> None:
            if stamp is not None and not (data := stamp.feed(data)):
                return
            await write(data)

        async with open_recording(playback, uri) as (head, chunks):
            args = ffmpeg_args(fmt, head.codec, offset=offset, duration=duration)
            await run_ffmpeg(playback_module.ffmpeg_binary(hass), args, chunks, send)
        if stamp is not None and (rest := stamp.flush()):
            await write(rest)

    return produce


class RecordingView(HomeAssistantView):
    """One recorded file, converted while it downloads, or its playlist to play on demand."""

    url = RECORDING_URL
    name = "api:hikvision_bridge:recording"
    requires_auth = True

    def __init__(self) -> None:
        self._spools = spool.Spools()

    async def get(
        self,
        request: web.Request,
        *,
        entry_id: str,
        channel_id: str,
        source: str,
        fmt: str,
        token: str,
    ) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        uri = decode_token(token)
        found = _find_source(hass, entry_id, channel_id, source)
        if uri is None or found is None or fmt not in (*FORMATS, PLAYBACK_SEEKABLE):
            return web.Response(status=404)
        _, playback = found
        if fmt == PLAYBACK_SEEKABLE:
            key = (entry_id, channel_id, playback.key)
            try:
                played = await vod.create(hass, key, playback, uri, request[KEY_HASS_USER].id)
            except vod.VodsFull:
                return web.Response(status=503)
            if played is None:
                return web.Response(status=404)
            return web.Response(
                text=played.playlist(), content_type=HLS, headers={"Cache-Control": "no-cache"}
            )
        offset = _seconds(request.query.get("offset"))
        duration = _seconds(request.query.get("duration"))
        headers = {"Content-Type": MIME[fmt]}
        if request.query.get("download"):
            name = f"recording_{channel_id}.{EXTENSION[fmt]}"
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        if fmt in MP4_FORMATS:
            key = (entry_id, channel_id, source, fmt, uri, offset, duration)
            return await self._spooled(
                request,
                hass=hass,
                playback=playback,
                key=key,
                fmt=fmt,
                uri=uri,
                offset=offset,
                duration=duration,
                headers=headers,
            )

        response = web.StreamResponse(headers=headers)

        async def send(data: bytes) -> None:
            # Started with the first data, so that earlier failures still get a status.
            if not response.prepared:
                await response.prepare(request)
            await response.write(data)

        try:
            async with open_recording(playback, uri) as (head, chunks):
                if fmt == FORMAT_ORIGINAL:
                    async for chunk in chunks:
                        await send(chunk)
                else:
                    args = ffmpeg_args(fmt, head.codec, offset=offset, duration=duration)
                    await run_ffmpeg(playback_module.ffmpeg_binary(hass), args, chunks, send)
            if not response.prepared:
                await response.prepare(request)
            await response.write_eof()
        except RecordingEncrypted:
            return web.Response(status=409, text="Encrypted recording; no verification code fits.")
        except NoRecording:
            if not response.prepared:
                return web.Response(status=502, text="No video in this recording.")
        except BridgeFault as err:
            _LOGGER.debug("Recording download failed: %s", type(err).__name__)
            if not response.prepared:
                return web.Response(status=502, text=type(err).__name__)
        except FileNotFoundError:
            return web.Response(status=503, text="ffmpeg is not available.")
        except ConnectionResetError:
            # The player went away (closed, or seeking with a new request).
            pass
        return response

    async def _spooled(
        self,
        request: web.Request,
        *,
        hass: HomeAssistant,
        playback: PlaybackSource,
        key: tuple,
        fmt: str,
        uri: str,
        offset: float | None,
        duration: float | None,
        headers: dict[str, str],
    ) -> web.StreamResponse:
        """Streamed MP4: converted into a spool that byte ranges are answered from (see spool)."""
        try:
            found = self._spools.get(key)
            if found is None:
                # Streamed MP4 has no length in its header; players then show one that grows.
                length = await _length(playback, uri, offset, duration)
                # Asked for again while the length was looked up: that spool serves the request.
                found = self._spools.get(key) or self._spools.open(
                    key,
                    _converter(
                        hass,
                        playback,
                        uri=uri,
                        fmt=fmt,
                        offset=offset,
                        duration=duration,
                        length=length,
                    ),
                )
            return await _spool_reply(request, found, headers)
        except RecordingEncrypted:
            return web.Response(status=409, text="Encrypted recording; no verification code fits.")
        except NoRecording:
            return web.Response(status=502, text="No video in this recording.")
        except BridgeFault as err:
            _LOGGER.debug("Recording download failed: %s", type(err).__name__)
            return web.Response(status=502, text=type(err).__name__)
        except FileNotFoundError:
            return web.Response(status=503, text="ffmpeg is not available.")
        except spool.SpoolsFull:
            return web.Response(status=503, text="Too many recordings are being converted.")


class ThumbnailView(HomeAssistantView):
    """First picture of a recorded file.

    Pictures are cached per recorded file, bounded by count and by bytes. A file without a
    picture (encrypted, or no video in it) is remembered as an empty entry, so that opening
    the clip list again does not download it again. Failures that may pass (a busy device,
    a timeout) are not remembered.
    """

    url = THUMBNAIL_URL
    name = "api:hikvision_bridge:thumbnail"
    requires_auth = True

    def __init__(self) -> None:
        # Key -> picture; b"" for a file without one.
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._gate = asyncio.Semaphore(THUMBNAIL_PARALLEL)

    def _remember(self, key: str, image: bytes) -> None:
        self._cache[key] = image
        self._cache.move_to_end(key)
        while len(self._cache) > THUMBNAIL_CACHE or (
            sum(len(value) for value in self._cache.values()) > THUMBNAIL_CACHE_BYTES
        ):
            self._cache.popitem(last=False)

    async def _picture(self, hass: HomeAssistant, playback: PlaybackSource, uri: str) -> bytes:
        """The picture of a recorded file; b"" if it has none. Raises for failures to retry."""
        try:
            async with (
                asyncio.timeout(THUMBNAIL_TIMEOUT),
                self._gate,
                open_recording(playback, uri) as (_, chunks),
            ):
                return await run_ffmpeg(
                    playback_module.ffmpeg_binary(hass), thumbnail_args(), chunks
                )
        except (RecordingEncrypted, NoRecording):
            return b""

    async def get(
        self, request: web.Request, entry_id: str, channel_id: str, source: str, token: str
    ) -> web.Response:
        hass = request.app[KEY_HASS]
        key = f"{entry_id}/{channel_id}/{source}/{token}"
        uri = decode_token(token)
        found = _find_source(hass, entry_id, channel_id, source)
        if uri is None or found is None:
            return web.Response(status=404)
        if (cached := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            if not cached:
                return web.Response(status=404)
            return web.Response(
                body=cached, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
            )
        _, playback = found
        try:
            image = await self._picture(hass, playback, uri)
        except (BridgeFault, FileNotFoundError, TimeoutError) as err:
            _LOGGER.debug("No thumbnail for now: %s", type(err).__name__)
            return web.Response(status=404)
        self._remember(key, image)
        if not image:
            return web.Response(status=404)
        return web.Response(
            body=image, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"}
        )


class SessionView(HomeAssistantView):
    """Playlist and segments of a playback session.

    Players fetch these without the login header; the random session id is the access
    token, like Home Assistant's own HLS streams.
    """

    url = "/api/hikvision_bridge/session/{session_id}/{name}"
    name = "api:hikvision_bridge:session"
    requires_auth = False

    async def get(self, request: web.Request, session_id: str, name: str) -> web.StreamResponse:
        if not sessions.FILE_NAME.match(name):
            return web.Response(status=404)
        hass = request.app[KEY_HASS]
        session = sessions.manager(hass).get(session_id, name)
        if session is None:
            return web.Response(status=404)
        # The URL is the token, but it dies with the user who started the session.
        owner = await hass.auth.async_get_user(session.owner) if session.owner else None
        if owner is None or not owner.is_active:
            hass.async_create_task(sessions.manager(hass).stop(session_id))
            return web.Response(status=404)
        path = session.folder / name
        if not await request.app[KEY_HASS].async_add_executor_job(path.is_file):
            return web.Response(status=404)
        content_type = {
            "m3u8": "application/vnd.apple.mpegurl",
            "m4s": "video/iso.segment",
            "mp4": "video/mp4",
            "ts": "video/mp2t",
        }[name.rsplit(".", 1)[1]]
        return web.FileResponse(
            path, headers={"Content-Type": content_type, "Cache-Control": "no-cache"}
        )


class VodView(HomeAssistantView):
    """Init section and parts of a recorded file played on demand.

    Like SessionView, the random token in the URL is the access token.
    """

    url = vod.URL
    name = "api:hikvision_bridge:vod"
    requires_auth = False

    async def get(self, request: web.Request, token: str, name: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        played = vod.get(hass, token)
        number = int(match[1]) if (match := _VOD_PART.match(name)) else None
        if played is None or (name != vod.INIT and (number is None or number >= played.parts)):
            return web.Response(status=404)
        owner = await hass.auth.async_get_user(played.owner)
        found = _find_source(hass, *played.key)
        if owner is None or not owner.is_active:
            vod.drop(hass, token)
            return web.Response(status=404)
        if found is None:
            return web.Response(status=404)
        bridge, source = found
        main = bridge.profile.channels[played.key[1]].main_stream
        codec = main.codec if main else None
        response: web.StreamResponse | None = None
        try:
            if number is None:
                init = await played.init_section(hass, source, codec)
                return (
                    web.Response(status=503)
                    if init is None
                    else web.Response(body=init, content_type="video/mp4")
                )
            async for data in played.part(hass, source, codec, number):
                if response is None:
                    response = web.StreamResponse(
                        headers={"Content-Type": "video/iso.segment", "Cache-Control": "no-cache"}
                    )
                    await response.prepare(request)
                await response.write(data)
        except (sessions.SessionError, BridgeFault, FileNotFoundError) as err:
            _LOGGER.debug(
                "No video on demand: %s",
                err.reason if isinstance(err, sessions.SessionError) else detail(err),
            )
            if response is not None:
                # Cut short: the player must not keep half a part, it asks again.
                _abort(request)
                return response
        except ConnectionResetError:
            # The player went away (seeked).
            return response or web.Response(status=503)
        if response is None:
            return web.Response(status=503)
        await response.write_eof()
        return response


def _abort(request: web.Request) -> None:
    """End a response sent in part without ending it properly, so it reads as failed."""
    if request.transport is not None:
        request.transport.close()


def async_register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(RecordingView())
    hass.http.register_view(ThumbnailView())
    hass.http.register_view(SessionView())
    hass.http.register_view(VodView())
