"""Playback sessions: recordings from a point in time, made into HLS by ffmpeg.

The device hands out the recordings of a time span as a download, much faster than playing
them; it is decrypted if needed and fed to ffmpeg, which only repackages it. The download is
held back once the prepared video is far enough ahead of the player. Some recorders refuse
the download of a span but hand out their recorded files; a session then covers the file that
holds the wanted time and starts at the last key frame before it. The part of the file before
that is dropped as fast as the device sends it, without ffmpeg. Devices that refuse both play
the span over RTSP instead, in real time and without decryption.

One session covers one stretch of contiguous recordings, so a position in the video maps to
the recorded time exactly (start of the session plus the playing time). Cameras serve only
one playback at a time; a viewer that seeks therefore replaces its session, and the old
process ends before the new one starts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from weakref import WeakSet

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from ..model.features import recordings
from ..model.features.scrambling import ProgramStream
from ..wire.faults import DeviceRejected, Unsupported, detail
from ..wire.link import IsapiLink
from ..wire.scrub import scrub_text, scrubbed_logger
from . import playback
from .playback import NoRecording, PlaybackSource, RecordingEncrypted

_LOGGER = scrubbed_logger(__name__)

QUALITY_ORIGINAL = "original"
QUALITY_COMPATIBLE = "compatible"
QUALITIES = (QUALITY_ORIGINAL, QUALITY_COMPATIBLE)
SEGMENT_SECONDS = 2
FIRST_SEGMENT_TIMEOUT = 25
# Longest wait for the part of a recorded file before the wanted time; slower devices start
# playing from the latest key frame they reached.
SKIP_SECONDS = 30
# Download kept while no key frame was found yet; past it the file plays from its start.
SKIP_SEARCH_BYTES = 8 * 1024 * 1024
# ffmpeg gives up on an RTSP connection that sends nothing for this long (microseconds).
RTSP_TIMEOUT = 10_000_000
IDLE_SECONDS = 45
MAX_SESSIONS = 4
# Video prepared beyond the last segment the player fetched (players buffer ahead too).
READ_AHEAD = 60
# Recordings closer than this count as one stretch.
JOIN_GAP = timedelta(seconds=10)
MAX_STRETCH = timedelta(hours=3)
PLAYLIST = "index.m3u8"
SEGMENT = "seg{:05d}.m4s"
LOG = "ffmpeg.log"
PIPE = "pipe:0"
FILE_NAME = re.compile(r"^[\w.-]+\.(m3u8|m4s|mp4|ts)$")
_SEGMENT_NUMBER = re.compile(r"^seg(\d+)\.m4s$")
DATA_KEY = "hikvision_bridge_sessions"


class SessionError(Exception):
    """Playback could not start; ``reason`` is a translation key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _UseRtsp(Exception):
    """No download from the device; ``lasting`` if none will come, ``refused`` if it said no."""

    def __init__(self, lasting: bool, refused: bool = False) -> None:
        super().__init__(lasting)
        self.lasting = lasting
        self.refused = refused


@dataclass(slots=True)
class Stretch:
    """Contiguous recordings: where playback starts and where it ends."""

    start: datetime
    end: datetime
    kinds: set[str] = field(default_factory=set)


def stretches(found: list[recordings.Recording]) -> list[Stretch]:
    joined: list[Stretch] = []
    for item in sorted(found, key=lambda r: r.start):
        last = joined[-1] if joined else None
        if last and item.start - last.end <= JOIN_GAP and item.end - last.start <= MAX_STRETCH:
            last.end = max(last.end, item.end)
            last.kinds.add(item.kind)
        else:
            joined.append(Stretch(item.start, item.end, {item.kind}))
    return joined


def locate(found: list[recordings.Recording], moment: datetime) -> tuple[datetime, datetime] | None:
    """Start and end of playback for ``moment``; in a gap, the next recording."""
    for stretch in stretches(found):
        if stretch.end <= moment:
            continue
        return max(moment, stretch.start), stretch.end
    return None


def covering(found: list[recordings.Recording], moment: datetime) -> recordings.Recording | None:
    """The recorded file holding ``moment``; in a gap, the next one."""
    ordered = sorted(found, key=lambda r: r.start)
    return next((r for r in ordered if r.start <= moment < r.end), None) or next(
        (r for r in ordered if r.start > moment), None
    )


def playback_uri(
    source: PlaybackSource, start: datetime, end: datetime, wall: timedelta = timedelta(0)
) -> str:
    """The span on the device's wall clock (``wall`` ahead of UTC)."""
    return (
        f"rtsp://device/Streaming/tracks/{source.track}/"
        f"?starttime={recordings.compact(start, wall)}&endtime={recordings.compact(end, wall)}"
    )


def hls_args(url: str, quality: str, codec: str | None, target: Path) -> list[str]:
    """ffmpeg arguments; ``url`` is an RTSP address or PIPE for a program stream on stdin."""
    args = ["-hide_banner", "-loglevel", "error"]
    if url == PIPE:
        args += ["-f", "mpeg"]
    else:
        args += ["-rtsp_transport", "tcp", "-timeout", str(RTSP_TIMEOUT)]
    # The streams are known after the first key frame; the default waits five seconds.
    args += ["-analyzeduration", "2000000", "-i", url, "-map", "0:v:0", "-map", "0:a:0?"]
    if quality == QUALITY_COMPATIBLE:
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"scale='min({playback.H264_MAX_WIDTH},iw)':-2",
            "-g",
            str(SEGMENT_SECONDS * 25),
        ]
    else:
        args += ["-c:v", "copy"]
        if codec and ("265" in codec or codec == "hevc"):
            args += ["-tag:v", "hvc1"]
    args += ["-c:a", "aac", "-b:a", "64k"]
    args += [
        "-f",
        "hls",
        "-hls_time",
        str(SEGMENT_SECONDS),
        "-hls_list_size",
        "0",
        "-hls_segment_type",
        "fmp4",
        "-hls_playlist_type",
        "event",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        str(target / SEGMENT.replace("{:05d}", "%05d")),
        str(target / PLAYLIST),
    ]
    return args


def played_seconds(playlist: str, fetched: int) -> float:
    """Video length up to and including segment number ``fetched``."""
    total = 0.0
    duration = 0.0
    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            with suppress(ValueError):
                duration = float(line[8:].split(",", 1)[0])
        elif match := _SEGMENT_NUMBER.match(line.strip()):
            if int(match.group(1)) > fetched:
                break
            total += duration
    return total


async def skipped(
    stream: ProgramStream, chunks: AsyncIterator[bytes], skip: float
) -> tuple[bytes, float]:
    """Read ``chunks`` up to ``skip`` seconds and drop what lies before the last key frame.

    Returns the bytes from that key frame on and its playing time. Stops at the latest key
    frame reached after SKIP_SECONDS; without any key frame the file plays from its start.
    """
    began = time.monotonic()
    stream.key_frames = []
    kept = bytearray()
    kept_at = 0  # stream offset of kept[0]
    cut: float | None = None
    async for data in chunks:
        kept += data
        frames, stream.key_frames = stream.key_frames, []
        for at, seconds in frames:
            if cut is None or seconds <= skip:
                drop = min(at - kept_at, len(kept))
                del kept[:drop]
                kept_at += drop
                cut = seconds
        if cut is None:
            if len(kept) > SKIP_SEARCH_BYTES:
                break
        elif stream.seconds >= skip or time.monotonic() - began > SKIP_SECONDS:
            break
    stream.key_frames = None
    if cut is None:
        _LOGGER.debug("No key frame in the recorded file, playing it from its start")
        return recordings.strip_header(bytes(kept)), 0.0
    _LOGGER.debug(
        "Recorded file plays from %.0f s (wanted %.0f s), reached in %.1f s",
        cut,
        skip,
        time.monotonic() - began,
    )
    return bytes(kept), cut


async def _joined(first: bytes, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    async for data in chunks:
        yield data


def _reason(errors: str) -> str:
    text = errors.lower()
    if "dimensions not set" in text or "invalid data" in text:
        return "playback_encrypted"
    if "453" in text or "not enough bandwidth" in text:
        return "playback_busy"
    if "401" in text or "unauthorized" in text:
        return "playback_auth"
    return "playback_failed"


@dataclass(slots=True)
class Session:
    session_id: str
    source_key: tuple[str, str, str]
    start: datetime
    end: datetime
    quality: str
    folder: Path
    process: asyncio.subprocess.Process | None = None
    pump: asyncio.Task | None = None
    last_access: float = field(default_factory=time.monotonic)
    fetched: int = -1  # highest segment number the player asked for

    @property
    def url(self) -> str:
        return f"/api/hikvision_bridge/session/{self.session_id}/{PLAYLIST}"

    def touch(self, name: str | None = None) -> None:
        self.last_access = time.monotonic()
        if name and (match := _SEGMENT_NUMBER.match(name)):
            self.fetched = max(self.fetched, int(match.group(1)))


class SessionManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._unsub: CALLBACK_TYPE | None = None
        self._rtsp_only: WeakSet[IsapiLink] = WeakSet()
        # Devices that refuse the download of a span but hand out recorded files.
        self._by_file: WeakSet[IsapiLink] = WeakSet()

    def get(self, session_id: str, name: str | None = None) -> Session | None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.touch(name)
        return session

    async def start(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        quality: str,
        codec: str | None,
        replace: str | None = None,
        found: list[recordings.Recording] | None = None,
    ) -> Session:
        """Play from ``start``; ``found`` are the recorded files around it."""
        async with self._lock:
            if replace:
                await self._stop(replace)
            # A camera plays one recording at a time; a new view of it ends the old one.
            for other in [s for s in self.sessions.values() if s.source_key == key]:
                await self._stop(other.session_id)
            while len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.last_access)
                await self._stop(oldest.session_id)
            uri = playback_uri(source, start, end, await recordings.wall_offset(source.link))
            folder = Path(
                await self.hass.async_add_executor_job(tempfile.mkdtemp, "", "hikvision_")
            )
            try:
                if source.link not in self._rtsp_only:
                    try:
                        session = await self._download(
                            source, key, start, end, uri, quality, folder, found or []
                        )
                    except _UseRtsp as err:
                        if err.lasting:
                            self._rtsp_only.add(source.link)
                    else:
                        self.sessions[session.session_id] = session
                        self._watch()
                        return session
                session = await self._from_rtsp(
                    source, key, start, end, uri, quality, codec, folder
                )
            except BaseException:
                await self.hass.async_add_executor_job(shutil.rmtree, folder, True)
                raise
            self.sessions[session.session_id] = session
            self._watch()
            return session

    # ------------------------------------------------------------- download

    async def _download(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        folder: Path,
        found: list[recordings.Recording],
    ) -> Session:
        """A session from the download of the span, else of the recorded file at ``start``."""
        recording = covering(found, start)
        if source.link not in self._by_file:
            try:
                return await self._from_download(
                    source, key, start, end, uri, quality, folder, "the time span"
                )
            except _UseRtsp as err:
                if not err.refused or recording is None:
                    raise
                self._by_file.add(source.link)
        if recording is None:
            raise _UseRtsp(False)
        start = max(start, recording.start)
        return await self._from_download(
            source,
            key,
            start,
            min(end, recording.end),
            recording.playback_uri,
            quality,
            folder,
            "the recorded file",
            (start - recording.start).total_seconds(),
        )

    async def _from_download(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        folder: Path,
        what: str,
        skip: float = 0.0,
    ) -> Session:
        session = Session(secrets.token_urlsafe(16), key, start, end, quality, folder)
        ready: asyncio.Future[None] = self.hass.loop.create_future()
        session.pump = self.hass.async_create_background_task(
            self._pump(session, source, uri, ready, skip),
            "hikvision_bridge playback download",
        )
        wait = FIRST_SEGMENT_TIMEOUT + (SKIP_SECONDS if skip > 0 else 0)
        try:
            await asyncio.wait_for(asyncio.shield(ready), wait)
        except BaseException as err:
            await _cancel(session.pump)
            if isinstance(err, RecordingEncrypted):
                raise SessionError("playback_encrypted") from err
            if isinstance(err, TimeoutError):
                _LOGGER.debug("Playback of %s did not start: no data in %s s", what, wait)
                raise SessionError("playback_failed") from err
            if isinstance(err, FileNotFoundError) or not isinstance(err, Exception):
                raise
            _LOGGER.debug("No download of %s: %s", what, detail(err))
            # Only a refusal is lasting; a dropped connection may work next time.
            raise _UseRtsp(
                isinstance(err, (DeviceRejected, Unsupported, NoRecording)),
                isinstance(err, DeviceRejected),
            ) from err
        reason = await self._first_segment(session, what)
        if reason is not None:
            await self._end(session)
            raise SessionError(reason)
        _LOGGER.debug("Playing %s", what)
        return session

    async def _pump(
        self,
        session: Session,
        source: PlaybackSource,
        uri: str,
        ready: asyncio.Future[None],
        skip: float = 0.0,
    ) -> None:
        """Download, decrypt and hand the span to ffmpeg, keeping READ_AHEAD in front.

        With ``skip`` the video starts at the last key frame before that many seconds, and
        the session start moves back to that frame.
        """
        stdin = None
        try:
            async with recordings.open_download(source.link, uri) as response:
                head = await playback.read_head(response)
                info, stream = await playback.prepare(source, head)
                chunks = playback.passed(stream, head, response)
                cut = 0.0
                if skip > 0:
                    first, cut = await skipped(stream, chunks, skip)
                    session.start -= timedelta(seconds=skip - cut)
                    chunks = _joined(first, chunks)
                else:
                    chunks = playback.stripped(chunks)
                session.process = await self._spawn(
                    session, hls_args(PIPE, session.quality, info.codec, session.folder), True
                )
                stdin = session.process.stdin
                assert stdin is not None
                ready.set_result(None)
                played = (-1, 0.0)
                async for data in chunks:
                    stdin.write(data)
                    await stdin.drain()
                    while stream.seconds - cut > played[1] + READ_AHEAD:
                        if session.fetched != played[0]:
                            text = await self.hass.async_add_executor_job(
                                _read, session.folder / PLAYLIST
                            )
                            played = (session.fetched, played_seconds(text or "", session.fetched))
                            continue
                        await asyncio.sleep(1)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as err:  # noqa: BLE001 - handed to the waiting start
            if not ready.done():
                ready.set_exception(err)
            elif not isinstance(err, (BrokenPipeError, ConnectionResetError)):
                # The playlist ends here; the player starts over from that point.
                _LOGGER.debug("Playback download ended early: %s", type(err).__name__)
        finally:
            if stdin is not None:
                with suppress(BrokenPipeError, ConnectionResetError, OSError):
                    stdin.close()

    # ------------------------------------------------------------- RTSP

    async def _from_rtsp(
        self,
        source: PlaybackSource,
        key: tuple[str, str, str],
        start: datetime,
        end: datetime,
        uri: str,
        quality: str,
        codec: str | None,
        folder: Path,
    ) -> Session:
        url = playback.rtsp_url(source, uri)
        reason = "playback_failed"
        for attempt in range(2):
            session = Session(secrets.token_urlsafe(16), key, start, end, quality, folder)
            session.process = await self._spawn(
                session, hls_args(url, quality, codec, folder), False
            )
            reason = await self._first_segment(session, "RTSP")
            if reason is None:
                _LOGGER.debug("Playing over RTSP")
                return session
            if reason != "playback_busy" or attempt:
                break
            await asyncio.sleep(1.5)  # the device releases the previous playback late
        raise SessionError(reason)

    # ------------------------------------------------------------- common

    async def _spawn(
        self, session: Session, args: list[str], piped: bool
    ) -> asyncio.subprocess.Process:
        # Errors go to a file: a pipe nobody reads would stall a long session.
        log = await self.hass.async_add_executor_job((session.folder / LOG).open, "wb")
        try:
            return await asyncio.create_subprocess_exec(
                playback.ffmpeg_binary(self.hass),
                *args,
                stdin=asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log,
            )
        finally:
            await self.hass.async_add_executor_job(log.close)

    async def _first_segment(self, session: Session, what: str) -> str | None:
        """None once the first segment is written, else why ffmpeg gave up."""
        assert session.process is not None
        playlist = session.folder / PLAYLIST
        deadline = time.monotonic() + FIRST_SEGMENT_TIMEOUT
        while time.monotonic() < deadline:
            text = await self.hass.async_add_executor_job(_read, playlist)
            if text and "#EXTINF" in text:
                return None
            if session.process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        running = session.process.returncode is None
        await self._end(session)
        errors = await self.hass.async_add_executor_job(_read, session.folder / LOG) or ""
        _LOGGER.debug(
            "Playback of %s did not start: %s%s",
            what,
            f"no video in {FIRST_SEGMENT_TIMEOUT} s"
            if running
            else f"ffmpeg ended with {session.process.returncode}",
            f"; ffmpeg: {scrub_text(errors[-400:])}" if errors else "",
        )
        return _reason(errors) if errors else "playback_failed"

    async def _end(self, session: Session) -> None:
        await _cancel(session.pump)
        if session.process is not None:
            await _end(session.process)

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            await self._stop(session_id)

    async def _stop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        await self._end(session)
        await self.hass.async_add_executor_job(shutil.rmtree, session.folder, True)

    async def stop_all(self) -> None:
        async with self._lock:
            for session_id in list(self.sessions):
                await self._stop(session_id)
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _watch(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self.hass, self._reap, timedelta(seconds=10))

    @callback
    def _reap(self, _now: datetime) -> None:
        idle = [
            s.session_id
            for s in self.sessions.values()
            if time.monotonic() - s.last_access > IDLE_SECONDS
        ]
        for session_id in idle:
            self.hass.async_create_task(self.stop(session_id))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _end(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def manager(hass: HomeAssistant) -> SessionManager:
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = SessionManager(hass)
    return hass.data[DATA_KEY]
