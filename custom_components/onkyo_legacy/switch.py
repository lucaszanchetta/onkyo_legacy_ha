"""Switch platform for optional speaker toggles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData, OnkyoZoneRuntimeData

DESCRIPTIONS = (
    ("cinema_filter", "Cinema Filter", "RAS", "cinema_filter"),
    ("audyssey_dynamic_eq", "Audyssey Dynamic EQ", "ADQ", "audyssey_dynamic_eq"),
    ("music_optimizer", "Music Optimizer", "MOT", "music_optimizer"),
    ("trigger_a", "12V Trigger A", "TGA", "trigger_a"),
    ("trigger_b", "12V Trigger B", "TGB", "trigger_b"),
    ("trigger_c", "12V Trigger C", "TGC", "trigger_c"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities from a config entry."""
    runtime: OnkyoRuntimeData = hass.data[DOMAIN][entry.entry_id]

    entities: list[CoordinatorEntity] = [
        OnkyoLegacyPowerSwitch(runtime),
        OnkyoLegacyMuteSwitch(runtime),
    ] + [
        OnkyoLegacySpeakerSwitch(runtime, key, name, command, state_attr)
        for key, name, command, state_attr in DESCRIPTIONS
        if runtime.coordinator.supports(command)
    ]

    if entities:
        async_add_entities(entities)


class OnkyoLegacySpeakerSwitch(CoordinatorEntity, SwitchEntity):
    """Represent a query-backed Onkyo binary control."""

    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: OnkyoRuntimeData | OnkyoZoneRuntimeData,
        key: str,
        name: str,
        command: str,
        state_attr: str,
    ) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._command = command
        self._state_attr = state_attr
        self._attr_name = name if runtime.zone_key == "main" else f"{runtime.zone_label} {name}"
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-{key}"
        self._attr_device_info = runtime.device_info
        self._attr_icon = {
            "cinema_filter": "mdi:movie-filter",
            "audyssey_dynamic_eq": "mdi:equalizer",
            "music_optimizer": "mdi:music",
            "trigger_a": "mdi:power-plug-outline",
            "trigger_b": "mdi:power-plug-outline",
            "trigger_c": "mdi:power-plug-outline",
        }.get(key, "mdi:toggle-switch")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def is_on(self) -> bool | None:
        return getattr(self.coordinator.data, self._state_attr)

    @property
    def available(self) -> bool:
        feature_supported = self.coordinator.supports(self._command)
        current_value = getattr(self.coordinator.data, self._state_attr)
        return self.coordinator.last_update_success and feature_supported and current_value is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._command.startswith("TG"):
            await self.coordinator.async_set_trigger(self._command, True)
            return
        await self.coordinator.async_set_boolean_option(self._command, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._command.startswith("TG"):
            await self.coordinator.async_set_trigger(self._command, False)
            return
        await self.coordinator.async_set_boolean_option(self._command, False)


class OnkyoLegacyPowerSwitch(CoordinatorEntity, SwitchEntity):
    """Represent the receiver power as a separate switch."""

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_icon = "mdi:power"

    def __init__(self, runtime: OnkyoRuntimeData | OnkyoZoneRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_name = "Power" if runtime.zone_key == "main" else f"{runtime.zone_label} Power"
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-power"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.power

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off()


class OnkyoLegacyMuteSwitch(CoordinatorEntity, SwitchEntity):
    """Represent audio mute as a separate switch."""

    _attr_has_entity_name = True
    _attr_name = "Mute"
    _attr_icon = "mdi:volume-mute"

    def __init__(self, runtime: OnkyoRuntimeData | OnkyoZoneRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_name = "Mute" if runtime.zone_key == "main" else f"{runtime.zone_label} Mute"
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-mute"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.muted

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_muted(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_muted(False)
