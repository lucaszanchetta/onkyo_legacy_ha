"""Switch platform for optional speaker toggles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData


@dataclass(frozen=True, slots=True)
class SpeakerDescription:
    key: str
    name: str
    command: str
    state_attr: str
    support_attr: str


DESCRIPTIONS = (
    SpeakerDescription("speaker_a", "Speaker A", "SPA", "speaker_a", "supports_speaker_a"),
    SpeakerDescription("speaker_b", "Speaker B", "SPB", "speaker_b", "supports_speaker_b"),
    SpeakerDescription("trigger_a", "12V Trigger A", "TGA", "trigger_a", "supports_trigger_a"),
    SpeakerDescription("trigger_b", "12V Trigger B", "TGB", "trigger_b", "supports_trigger_b"),
    SpeakerDescription("trigger_c", "12V Trigger C", "TGC", "trigger_c", "supports_trigger_c"),
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up switch entities from discovery info."""
    if not discovery_info:
        return

    runtime: OnkyoRuntimeData = hass.data[DOMAIN][discovery_info["runtime_id"]]

    entities: list[OnkyoLegacySpeakerSwitch] = []
    for description in DESCRIPTIONS:
        if getattr(runtime.coordinator, description.support_attr):
            entities.append(OnkyoLegacySpeakerSwitch(runtime, description))

    if entities:
        async_add_entities(entities)


class OnkyoLegacySpeakerSwitch(CoordinatorEntity, SwitchEntity):
    """Represent an optional speaker on/off switch."""

    _attr_has_entity_name = True

    def __init__(self, runtime: OnkyoRuntimeData, description: SpeakerDescription) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{runtime.host}-{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, runtime.host)},
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def is_on(self) -> bool | None:
        return getattr(self.coordinator.data, self._description.state_attr)

    @property
    def available(self) -> bool:
        feature_supported = getattr(self.coordinator, self._description.support_attr)
        current_value = getattr(self.coordinator.data, self._description.state_attr)
        return self.coordinator.last_update_success and feature_supported and current_value is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._description.command.startswith("TG"):
            await self.coordinator.async_set_trigger(self._description.command, True)
            return
        await self.coordinator.async_set_speaker(self._description.command, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._description.command.startswith("TG"):
            await self.coordinator.async_set_trigger(self._description.command, False)
            return
        await self.coordinator.async_set_speaker(self._description.command, False)
