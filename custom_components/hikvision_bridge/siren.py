"""Alarm siren of cameras with an audible alarm: on plays the siren sound until it is turned off."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.siren import SirenEntity, SirenEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import BridgeEntity, add_entities
from .model.features import signals
from .runtime.bridge import DeviceBridge
from .wire.faults import BridgeFault, DeviceRejected, detail

PARALLEL_UPDATES = 1
# Seconds between triggers while the siren is on. A trigger that comes while the sound still
# plays is refused by the camera, which counts as playing.
RETRIGGER_SECONDS = 1.0
# Failed triggers in a row before the siren gives up and reports itself off.
MAX_FAILURES = 3

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    entities = [
        ChannelSiren(bridge, channel.channel_id)
        for channel in bridge.profile.channels.values()
        if channel.siren
    ]
    add_entities(bridge, async_add_entities, entities)


def _still_playing(err: BridgeFault) -> bool:
    """The camera refused a trigger because the sound is still playing."""
    if not isinstance(err, DeviceRejected) or err.status != 403:
        return False
    return "playing" in f"{err} {err.sub_status or ''}".casefold()


class ChannelSiren(BridgeEntity, SirenEntity):
    """The camera's audible alarm. The camera has no stop request, so off stops re-triggering."""

    _attr_should_poll = False
    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    _attr_translation_key = "alarm"

    def __init__(self, bridge: DeviceBridge, channel_id: str) -> None:
        super().__init__(bridge, channel_id, "siren")
        self._sounding: asyncio.Task[None] | None = None

    @property
    def is_on(self) -> bool:
        return self._sounding is not None

    async def _play(self) -> None:
        link, _ = self.bridge.route(self.channel_id)
        await signals.trigger_siren(link)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on:
            return
        try:
            await self._play()
        except BridgeFault as err:
            # A sound that is still playing (say, from before a restart) counts as on.
            if not _still_playing(err):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_failed",
                    translation_placeholders={"reason": type(err).__name__},
                ) from err
        self._sounding = self.hass.async_create_background_task(
            self._keep_sounding(), f"{DOMAIN} siren {self.channel_id}"
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._stop()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._stop()
        await super().async_will_remove_from_hass()

    def _stop(self) -> None:
        if self._sounding is not None:
            self._sounding.cancel()
            self._sounding = None

    async def _keep_sounding(self) -> None:
        failures = 0
        while True:
            await asyncio.sleep(RETRIGGER_SECONDS)
            try:
                await self._play()
            except BridgeFault as err:
                if _still_playing(err):
                    failures = 0
                    continue
                failures += 1
                _LOGGER.debug("Siren trigger failed: %s (%d in a row)", detail(err), failures)
                if failures >= MAX_FAILURES:
                    _LOGGER.warning("Siren of channel %s stopped after failures", self.channel_id)
                    self._sounding = None
                    self.async_write_ha_state()
                    return
            else:
                failures = 0
