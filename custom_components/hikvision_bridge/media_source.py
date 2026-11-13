"""Recordings of recorders and memory cards in the media browser.

One folder per device, named by its config entry id, then one per channel with recordings, named
by the channel's Home Assistant device id, then its days: ``entry/device/day`` lists the files that
start on the day, ``entry/device/day/kind`` those of one kind, ``entry/device/clip/token`` is one
recorded file. A recorder's channels play from the recorder, cameras from their memory card.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTRY_KIND,
    DOMAIN,
    KIND_CLOUD,
    OPT_PLAYBACK,
    OPT_RTSP_TRANSPORT,
    PLAYBACK_SEEKABLE,
    PLAYBACK_STREAM,
    RTSP_TCP,
)
from .model.features import recordings
from .runtime import identity
from .runtime.bridge import DeviceBridge
from .runtime.playback import (
    FORMAT_MP4,
    MIME,
    PlaybackSource,
    rtsp_url,
    sources,
)
from .views import decode_token, encode_token, recording_url, thumbnail_url
from .wire.faults import BridgeFault
from .wire.scrub import scrubbed_logger

_LOGGER = scrubbed_logger(__name__)

DAYS_WITHOUT_CALENDAR = 14
MAX_DAYS = 62
# Thumbnails cost one download each; continuous recording days have too many files.
MAX_THUMBNAILS = 60
# Home Assistant's browser player only plays HLS announced exactly as this type.
PLAY_HLS = "application/x-mpegURL"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return BridgeMediaSource(hass)


def _folder(identifier: str, title: str, **kwargs) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=True,
        children_media_class=kwargs.pop("children_media_class", MediaClass.DIRECTORY),
        **kwargs,
    )


class BridgeMediaSource(MediaSource):
    name = "Hikvision Bridge"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ------------------------------------------------------------- helpers

    def _channels(self) -> dict[str, tuple[DeviceBridge, str]]:
        """Channels with recordings of every loaded device, by their device id."""
        found = {}
        for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            bridge = getattr(entry, "runtime_data", None)
            if entry.data.get(CONF_ENTRY_KIND) == KIND_CLOUD or not isinstance(
                bridge, DeviceBridge
            ):
                continue
            for channel_id in bridge.profile.channels:
                device_id = bridge.device_ids.get(channel_id)
                if device_id and sources(bridge, channel_id):
                    found[device_id] = (bridge, channel_id)
        return found

    async def _labels(self) -> dict[str, str]:
        texts = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.selector."
        return {
            key.removeprefix(prefix): value
            for key, value in texts.items()
            if key.startswith(prefix)
        }

    def _camera_thumbnail(self, bridge: DeviceBridge, channel_id: str) -> str | None:
        uid = identity.entity_uid(bridge.profile, channel_id, "stream_1")
        entity_id = er.async_get(self.hass).async_get_entity_id(Platform.CAMERA, DOMAIN, uid)
        return f"/api/camera_proxy/{entity_id}" if entity_id else None

    # -------------------------------------------------------------- browse

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = item.identifier.split("/") if item.identifier else []
        try:
            channels = self._channels()
            if not parts:
                return self._root(channels)
            if len(parts) == 1:
                return self._device(parts[0], channels)
            bridge, channel_id = _channel(channels, parts[0], parts[1])
            if len(parts) > 4:
                raise BrowseError("Unknown media item")
            folder = f"{parts[0]}/{parts[1]}"
            # The recorder if the device records, else the camera's own memory card.
            source = sources(bridge, channel_id)[0]
            if len(parts) == 2:
                return await self._days(folder, bridge, channel_id, source)
            day = date.fromisoformat(parts[2])
            kind = parts[3] if len(parts) == 4 else None
            if kind not in (None, *recordings.KINDS):
                raise BrowseError("Unknown media item")
            return await self._day(folder, bridge, channel_id, source, day, kind)
        except BridgeFault as err:
            raise BrowseError(f"Device not reachable ({type(err).__name__})") from err
        except ValueError as err:
            raise BrowseError("Unknown media item") from err

    def _root(self, channels: dict[str, tuple[DeviceBridge, str]]) -> BrowseMediaSource:
        base = _folder("", self.name)
        base.identifier = None
        entries = {bridge.entry.entry_id: bridge.entry.title for bridge, _ in channels.values()}
        base.children = [_folder(entry_id, title) for entry_id, title in entries.items()]
        return base

    def _device(
        self, entry_id: str, channels: dict[str, tuple[DeviceBridge, str]]
    ) -> BrowseMediaSource:
        own = {d: c for d, c in channels.items() if c[0].entry.entry_id == entry_id}
        if not own:
            raise BrowseError("Unknown media item")
        base = _folder(entry_id, next(iter(own.values()))[0].entry.title)
        base.children = [
            _folder(
                f"{entry_id}/{device_id}",
                bridge.profile.channels[channel_id].name,
                thumbnail=self._camera_thumbnail(bridge, channel_id),
            )
            for device_id, (bridge, channel_id) in own.items()
        ]
        return base

    async def _days(
        self, folder: str, bridge: DeviceBridge, channel_id: str, source: PlaybackSource
    ) -> BrowseMediaSource:
        base = _folder(folder, bridge.profile.channels[channel_id].name)
        today = dt_util.now().date()
        days: list[date] = []
        months = {(today.year, today.month)}
        earlier = today.replace(day=1) - timedelta(days=1)
        months.add((earlier.year, earlier.month))
        calendar = True
        for year, month in sorted(months, reverse=True):
            found = await recordings.recorded_days(source.link, source.track, year, month)
            if found is None:
                calendar = False
                break
            days.extend(date(year, month, d) for d in found if date(year, month, d) <= today)
        if not calendar:
            days = [today - timedelta(days=n) for n in range(DAYS_WITHOUT_CALENDAR)]
        base.children = [
            _folder(f"{folder}/{day.isoformat()}", day.isoformat())
            for day in sorted(days, reverse=True)[:MAX_DAYS]
        ]
        return base

    async def _day(
        self,
        folder: str,
        bridge: DeviceBridge,
        channel_id: str,
        source: PlaybackSource,
        day: date,
        kind: str | None,
    ) -> BrowseMediaSource:
        """The files that start on ``day``; with several kinds, a folder per kind as well."""
        labels = await self._labels()
        prefix = f"{folder}/{day.isoformat()}"
        start, end = _midnight(day), _midnight(day + timedelta(days=1))
        # Devices cut files at the ends of a search, playback URIs too. From a second before
        # midnight on, the file running into the day starts before it and is left out.
        searched = await recordings.search(
            source.link, source.track, start - timedelta(seconds=1), end
        )
        found = [r for r in searched if r.start >= start]
        # The files running past the day are searched on to their real end.
        crossing = [r for r in found if r.end >= end]
        if crossing and end <= dt_util.utcnow():
            # Most files end soon after midnight, and cameras with many files a day take long
            # to search one: a whole day more is searched only for files running past an hour.
            later: list[recordings.Recording] = []
            for reach in (timedelta(hours=1), timedelta(days=1)):
                later = [
                    r
                    for r in await recordings.search(
                        source.link, source.track, crossing[0].start, end + reach
                    )
                    if r.start < end <= r.end
                ]
                if all(r.end < end + reach for r in later):
                    break
            found = [r for r in found if r.end < end] + (later or crossing)
        kinds = sorted({r.kind for r in found}, key=recordings.KINDS.index)
        title = day.isoformat()
        folders = []
        if kind is not None:
            found = [r for r in found if r.kind == kind]
            prefix = f"{prefix}/{kind}"
            title = f"{title} - {labels.get(f'recording_kind.options.{kind}', kind)}"
        elif len(kinds) > 1:
            folders = [
                _folder(
                    f"{prefix}/{key}",
                    labels.get(f"recording_kind.options.{key}", key),
                    children_media_class=MediaClass.VIDEO,
                )
                for key in kinds
            ]
        base = _folder(prefix, title, children_media_class=MediaClass.VIDEO)
        thumbnails = len(found) <= MAX_THUMBNAILS
        base.children = folders + [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{folder}/clip/{encode_token(r.playback_uri)}",
                media_class=MediaClass.VIDEO,
                media_content_type=MIME[FORMAT_MP4],
                title=_clip_title(r, labels, show_kind=bool(folders)),
                can_play=True,
                can_expand=False,
                thumbnail=thumbnail_url(
                    bridge.entry.entry_id, channel_id, source.key, r.playback_uri
                )
                if thumbnails
                else None,
            )
            for r in sorted(found, key=lambda r: r.start, reverse=True)
        ]
        return base

    # ------------------------------------------------------------- resolve

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 4 or parts[2] != "clip":
            raise Unresolvable("Not a recording")
        entry_id, device_id, _, token = parts
        uri = decode_token(token)
        try:
            bridge, channel_id = _channel(self._channels(), entry_id, device_id)
        except BrowseError as err:
            raise Unresolvable("Unknown recording") from err
        if uri is None:
            raise Unresolvable("Unknown recording")
        source = sources(bridge, channel_id)[0]
        mode = bridge.entry.options.get(OPT_PLAYBACK, FORMAT_MP4)
        if mode == PLAYBACK_SEEKABLE:
            return PlayMedia(recording_url(entry_id, channel_id, source.key, mode, uri), PLAY_HLS)
        if mode == PLAYBACK_STREAM:
            try:
                return await self._stream(bridge, source, uri)
            except (HomeAssistantError, ImportError) as err:
                _LOGGER.debug("Stream playback unavailable, converting instead: %s", err)
                mode = FORMAT_MP4
        url = recording_url(entry_id, channel_id, source.key, mode, uri)
        return PlayMedia(url, MIME[mode])

    async def _stream(self, bridge: DeviceBridge, source: PlaybackSource, uri: str) -> PlayMedia:
        """Let the device play the recording over RTSP; Home Assistant serves it as HLS."""
        from homeassistant.components.stream import (  # noqa: PLC0415 - optional
            HLS_PROVIDER,
            create_stream,
        )

        try:
            # Home Assistant 2026.10 moved it from stream to camera.
            from homeassistant.components.camera import (  # noqa: PLC0415 - optional
                DynamicStreamSettings,
            )
        except ImportError:
            from homeassistant.components.stream import (  # noqa: PLC0415 - optional
                DynamicStreamSettings,
            )

        stream = create_stream(
            self.hass,
            rtsp_url(source, uri),
            {"rtsp_transport": bridge.entry.options.get(OPT_RTSP_TRANSPORT, RTSP_TCP)},
            DynamicStreamSettings(),
            "recording",
        )
        stream.add_provider(HLS_PROVIDER)
        await stream.start()
        return PlayMedia(stream.endpoint_url(HLS_PROVIDER), PLAY_HLS)


def _channel(
    channels: dict[str, tuple[DeviceBridge, str]], entry_id: str, device_id: str
) -> tuple[DeviceBridge, str]:
    """The bridge and channel of ``entry_id/device_id``."""
    found = channels.get(device_id)
    if found is None or found[0].entry.entry_id != entry_id:
        raise BrowseError("Unknown media item")
    return found


def _midnight(day: date) -> datetime:
    return dt_util.start_of_local_day(day)


def _clip_title(recording: recordings.Recording, labels: dict[str, str], *, show_kind: bool) -> str:
    """``HH:MM:SS - HH:MM:SS`` (an end before the start is on the next day), kind first."""
    start = dt_util.as_local(recording.start)
    # A file still recording is listed up to the end of the search.
    end = dt_util.as_local(min(recording.end, dt_util.utcnow()))
    title = f"{start:%H:%M:%S} - {end:%H:%M:%S}"
    if show_kind:
        label = labels.get(f"recording_kind.options.{recording.kind}", recording.kind)
        title = f"{label} · {title}"
    return title
