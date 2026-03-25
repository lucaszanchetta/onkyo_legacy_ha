"""Select platform for the Onkyo legacy integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the select entity from discovery info."""
    if not discovery_info:
        return

    runtime: OnkyoRuntimeData = hass.data[DOMAIN][discovery_info["runtime_id"]]
    entities: list[CoordinatorEntity] = []
    if runtime.coordinator.supports_listening_mode:
        entities.append(OnkyoLegacyListeningModeSelect(runtime))
    if runtime.coordinator.supports_dimmer:
        entities.append(OnkyoLegacyDimmerSelect(runtime))

    if entities:
        async_add_entities(entities)


class OnkyoLegacyListeningModeSelect(CoordinatorEntity, SelectEntity):
    """Represent a listening-mode select."""

    _attr_has_entity_name = True
    _attr_name = "Listening Mode"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-listening-mode"
        self._attr_options = runtime.supported_listening_modes
        self._attr_device_info = {
            "identifiers": {(DOMAIN, runtime.host)},
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.listening_mode

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports_listening_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_listening_mode(option)


class OnkyoLegacyDimmerSelect(CoordinatorEntity, SelectEntity):
    """Represent a dimmer-level select."""

    _attr_has_entity_name = True
    _attr_name = "Dimmer Level"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-dimmer-level"
        self._attr_options = ["bright", "dim", "dark", "shut-off", "bright-led-off"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, runtime.host)},
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.data.dimmer_level

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports_dimmer

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_dimmer_level(option)
