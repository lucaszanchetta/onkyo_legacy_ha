"""Number platform for query-backed theater tuning controls."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData, OnkyoZoneRuntimeData


@dataclass(frozen=True, slots=True)
class NumberDescription:
    key: str
    name: str
    command: str
    state_attr: str
    native_min_value: float
    native_max_value: float
    native_step: float


DESCRIPTIONS = (
    NumberDescription("sleep-timer", "Sleep Timer", "SLP", "sleep_minutes", 0, 90, 1),
    NumberDescription("center-level", "Center Temporary Level", "CTL", "center_level", -12, 12, 1),
    NumberDescription("subwoofer-level", "Subwoofer Temporary Level", "SWL", "subwoofer_level", -15, 12, 1),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    runtime: OnkyoRuntimeData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        OnkyoLegacyNumber(runtime, description)
        for description in DESCRIPTIONS
        if runtime.coordinator.supports(description.command)
    ]
    entities = [OnkyoLegacyVolumeNumber(zone_runtime) for zone_runtime in runtime.zones] + entities
    if entities:
        async_add_entities(entities)


class OnkyoLegacyNumber(CoordinatorEntity, NumberEntity):
    """Represent a query-backed numeric control."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime: OnkyoRuntimeData, description: NumberDescription) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-{description.key}"
        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step
        self._attr_device_info = runtime.device_info
        self._attr_icon = {
            "sleep-timer": "mdi:sleep",
            "center-level": "mdi:speaker-center",
            "subwoofer-level": "mdi:subwoofer",
        }.get(description.key, "mdi:numeric")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def native_value(self) -> float | None:
        value = getattr(self.coordinator.data, self._description.state_attr)
        return None if value is None else float(value)

    @property
    def available(self) -> bool:
        value = getattr(self.coordinator.data, self._description.state_attr)
        return self.coordinator.last_update_success and self.coordinator.supports(
            self._description.command
        ) and value is not None

    async def async_set_native_value(self, value: float) -> None:
        native_value = int(round(value))
        if self._description.command == "SLP":
            await self.coordinator.async_set_sleep_minutes(native_value)
            return
        await self.coordinator.async_set_level(self._description.command, native_value)


class OnkyoLegacyVolumeNumber(CoordinatorEntity, NumberEntity):
    """Represent the main volume as a separate number entity."""

    _attr_has_entity_name = True
    _attr_name = "Volume Level"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:volume-high"

    def __init__(self, runtime: OnkyoRuntimeData | OnkyoZoneRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_name = (
            "Volume Level" if runtime.zone_key == "main" else f"{runtime.zone_label} Volume Level"
        )
        self._attr_unique_id = f"{runtime.host}-{runtime.zone_key}-volume-level"
        self._attr_native_min_value = 0
        self._attr_native_max_value = runtime.max_volume
        self._attr_native_step = 1
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def native_value(self) -> float | None:
        volume = self.coordinator.data.volume
        return None if volume is None else float(volume)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data.volume is not None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_volume(value / self._runtime.max_volume, self._runtime.max_volume)
