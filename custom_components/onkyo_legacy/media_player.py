"""Media player platform for the Onkyo legacy integration."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the media player from discovery info."""
    if not discovery_info:
        return

    runtime: OnkyoRuntimeData = hass.data[DOMAIN][discovery_info["runtime_id"]]
    async_add_entities([OnkyoLegacyMediaPlayer(runtime)])


class OnkyoLegacyMediaPlayer(CoordinatorEntity, MediaPlayerEntity):
    """Represent the main-zone legacy Onkyo device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-main"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, runtime.host)},
            "name": runtime.title,
            "manufacturer": "Onkyo",
            "model": "PR-SC5507",
            "configuration_url": f"http://{runtime.host}",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def state(self) -> MediaPlayerState | str | None:
        if not self.available:
            return None
        return MediaPlayerState.ON if self.coordinator.data.power else MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        return min(self.coordinator.data.volume / self._runtime.max_volume, 1.0)

    @property
    def is_volume_muted(self) -> bool | None:
        return self.coordinator.data.muted

    @property
    def source(self) -> str | None:
        source = self.coordinator.data.source
        if source is None:
            return None
        return self._runtime.source_lookup.get(source.lower(), source)

    @property
    def source_list(self) -> list[str]:
        return list(self._runtime.sources.keys())

    async def async_turn_on(self) -> None:
        await self.coordinator.async_turn_on()

    async def async_turn_off(self) -> None:
        await self.coordinator.async_turn_off()

    async def async_mute_volume(self, mute: bool) -> None:
        await self.coordinator.async_set_muted(mute)

    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.async_set_volume(volume, self._runtime.max_volume)

    async def async_volume_up(self) -> None:
        await self.coordinator.async_volume_step(True)

    async def async_volume_down(self) -> None:
        await self.coordinator.async_volume_step(False)

    async def async_select_source(self, source: str) -> None:
        command_source = self._runtime.sources[source]
        await self.coordinator.async_select_source(command_source)
