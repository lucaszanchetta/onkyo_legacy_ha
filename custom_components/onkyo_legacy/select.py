"""Select platform for the Onkyo legacy integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData, OnkyoZoneRuntimeData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities from a config entry."""
    runtime: OnkyoRuntimeData = hass.data[DOMAIN][entry.entry_id]
    entities: list[CoordinatorEntity] = []
    for zone_runtime in runtime.zones:
        if zone_runtime.sources:
            entities.append(OnkyoLegacySourceSelect(zone_runtime))
    if runtime.coordinator.supports("LMD"):
        entities.append(OnkyoLegacyListeningModeSelect(runtime))
    if runtime.coordinator.supports("DIM"):
        entities.append(OnkyoLegacyDimmerSelect(runtime))
    if runtime.coordinator.supports("SLA"):
        entities.append(OnkyoLegacyAudioSelectorSelect(runtime))
    if runtime.coordinator.supports("LTN"):
        entities.append(OnkyoLegacyLateNightSelect(runtime))
    if runtime.coordinator.supports("ADV"):
        entities.append(OnkyoLegacyAudysseyDynamicVolumeSelect(runtime))

    if entities:
        async_add_entities(entities)


class OnkyoLegacySourceSelect(CoordinatorEntity, SelectEntity):
    """Represent a source select."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:source-branch"

    def __init__(self, runtime: OnkyoRuntimeData | OnkyoZoneRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_name = "Source" if runtime.zone_key == "main" else f"{runtime.zone_label} Source"
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-source"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        source = self.coordinator.data.source
        if source is None:
            return None
        return self._runtime.source_lookup.get(source.lower(), source)

    @property
    def options(self) -> list[str]:
        current = self.current_option
        options = list(self._runtime.sources.keys())
        if current and current not in options:
            options.append(current)
        return options

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_select_option(self, option: str) -> None:
        command_source = self._runtime.sources.get(option)
        if command_source is None:
            raise ValueError(f"Unsupported source option: {option}")
        await self.coordinator.async_select_source(command_source)


class OnkyoLegacyListeningModeSelect(CoordinatorEntity, SelectEntity):
    """Represent a listening-mode select."""

    _attr_has_entity_name = True
    _attr_name = "Listening Mode"
    _attr_icon = "mdi:surround-sound"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-listening-mode"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.listening_mode

    @property
    def options(self) -> list[str]:
        current = self.current_option
        options = list(self._runtime.supported_listening_modes)
        if current and current not in options:
            options.append(current)
        return options

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("LMD")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_listening_mode(option)


class OnkyoLegacyDimmerSelect(CoordinatorEntity, SelectEntity):
    """Represent a dimmer-level select."""

    _attr_has_entity_name = True
    _attr_name = "Dimmer Level"
    _attr_icon = "mdi:brightness-6"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-dimmer-level"
        self._attr_options = runtime.supported_dimmer_modes
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.dimmer_level

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("DIM")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_dimmer_level(option)


class OnkyoLegacyAudioSelectorSelect(CoordinatorEntity, SelectEntity):
    """Represent an audio-selector select."""

    _attr_has_entity_name = True
    _attr_name = "Audio Selector"
    _attr_icon = "mdi:audio-input-rca"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-audio-selector"
        self._attr_options = runtime.supported_audio_selectors
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.audio_selector

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("SLA")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_audio_selector(option)


class OnkyoLegacyLateNightSelect(CoordinatorEntity, SelectEntity):
    """Represent a late-night mode select."""

    _attr_has_entity_name = True
    _attr_name = "Late Night"
    _attr_icon = "mdi:weather-night"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-late-night"
        self._attr_options = runtime.supported_late_night_modes
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.late_night_mode

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("LTN")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_late_night_mode(option)


class OnkyoLegacyAudysseyDynamicVolumeSelect(CoordinatorEntity, SelectEntity):
    """Represent an Audyssey Dynamic Volume select."""

    _attr_has_entity_name = True
    _attr_name = "Audyssey Dynamic Volume"
    _attr_icon = "mdi:volume-high"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-audyssey-dynamic-volume"
        self._attr_options = runtime.supported_audyssey_volume_modes
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.audyssey_dynamic_volume

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("ADV")

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_audyssey_dynamic_volume(option)
