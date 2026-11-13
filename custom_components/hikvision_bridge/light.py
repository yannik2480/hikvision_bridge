"""White light of cameras whose supplement light has a white light mode (ColorVu, hybrid light).

On switches the supplement light to its white light mode, off switches it back to the mode it
had before (the smart mode after a restart). It is the same setting as the supplement light
select, so the camera still decides when the lamp is lit in that mode (typically at night).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from .const import DOMAIN
from .entity import PolledEntity, add_entities, async_write_setting
from .model.features.image import ImageCapability, ImageValue
from .runtime.bridge import DeviceBridge

PARALLEL_UPDATES = 1
MODE = "supplement_light"
LEVEL = "white_light_brightness"
# Device names of the white light mode, and the modes off goes back to when none is known.
WHITE_MODES = ("colorVuWhiteLight", "whiteLight")
OFF_MODES = ("eventIntelligence", "irLight", "close")


def white_mode(capability: ImageCapability | None) -> str | None:
    if capability is None:
        return None
    return next((option for option in capability.options if option in WHITE_MODES), None)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    bridge: DeviceBridge = entry.runtime_data
    entities = [
        WhiteLight(bridge, channel.channel_id, channel.image[MODE], channel.image.get(LEVEL))
        for channel in bridge.profile.channels.values()
        if white_mode(channel.image.get(MODE)) is not None
    ]
    add_entities(bridge, async_add_entities, entities)


class WhiteLight(PolledEntity, LightEntity):
    _attr_translation_key = "white_light"

    def __init__(
        self,
        bridge: DeviceBridge,
        channel_id: str,
        modes: ImageCapability,
        level: ImageCapability | None,
    ) -> None:
        super().__init__(bridge, channel_id, "white_light")
        self._modes = modes.options
        self._white = white_mode(modes) or ""
        self._span: tuple[int, int] | None = None
        if level is not None and level.minimum is not None and level.maximum is not None:
            self._span = (max(1, level.minimum), level.maximum)
        mode = ColorMode.BRIGHTNESS if self._span else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}
        # The mode the light had before it was turned on, to go back to on off.
        self._off_mode: str | None = None

    def _value(self, key: str) -> ImageValue | None:
        return self.coordinator.data.image.get(self.channel_id, {}).get(key)

    @property
    def available(self) -> bool:
        return super().available and self._value(MODE) is not None

    @property
    def is_on(self) -> bool | None:
        current = self._value(MODE)
        return None if current is None else current.value == self._white

    @property
    def brightness(self) -> int | None:
        level = self._value(LEVEL)
        if self._span is None or level is None:
            return None
        try:
            value = int(level.value)
        except ValueError:
            return None
        return value_to_brightness(self._span, max(self._span[0], value))

    async def async_turn_on(self, **kwargs: Any) -> None:
        current = self._value(MODE)
        if current is not None and current.value != self._white:
            self._off_mode = current.value
            await async_write_setting(self.bridge, self.channel_id, MODE, self._white)
        if self._span is None or ATTR_BRIGHTNESS not in kwargs:
            return
        if self._value(LEVEL) is None:
            # The brightness element only shows in the white light mode: read it now.
            self.bridge.refresher.request_full()
            await self.bridge.refresher.async_refresh()
        if self._value(LEVEL) is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="setting_inactive")
        value = round(brightness_to_value(self._span, kwargs[ATTR_BRIGHTNESS]))
        await async_write_setting(self.bridge, self.channel_id, LEVEL, str(value))

    async def async_turn_off(self, **kwargs: Any) -> None:
        current = self._value(MODE)
        if current is None or current.value != self._white:
            return
        await async_write_setting(self.bridge, self.channel_id, MODE, self._mode_for_off())

    def _mode_for_off(self) -> str:
        if self._off_mode in self._modes and self._off_mode != self._white:
            return self._off_mode
        others = [mode for mode in self._modes if mode != self._white]
        return next((mode for mode in OFF_MODES if mode in others), others[0] if others else "")
