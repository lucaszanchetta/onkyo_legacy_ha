"""Diagnostic sensor platform for parsed Onkyo signal metadata."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OnkyoRuntimeData


@dataclass(frozen=True, slots=True)
class DiagnosticSensorDescription(SensorEntityDescription):
    data_attr: str = ""
    key_name: str = ""


SENSOR_DESCRIPTIONS = (
    DiagnosticSensorDescription(
        key="audio_input_terminal",
        name="Audio Input Terminal",
        data_attr="audio_information",
        key_name="input_terminal",
        icon="mdi:input-hdmi",
    ),
    DiagnosticSensorDescription(
        key="audio_input_signal",
        name="Audio Input Signal",
        data_attr="audio_information",
        key_name="input_signal",
        icon="mdi:audio-video",
    ),
    DiagnosticSensorDescription(
        key="audio_sampling_frequency",
        name="Audio Sampling Frequency",
        data_attr="audio_information",
        key_name="sampling_frequency",
        icon="mdi:waveform",
    ),
    DiagnosticSensorDescription(
        key="audio_input_channels",
        name="Audio Input Channels",
        data_attr="audio_information",
        key_name="input_channels",
        icon="mdi:speaker-multiple",
    ),
    DiagnosticSensorDescription(
        key="audio_listening_mode",
        name="Audio Listening Mode",
        data_attr="audio_information",
        key_name="listening_mode",
        icon="mdi:surround-sound",
    ),
    DiagnosticSensorDescription(
        key="video_input",
        name="Video Input",
        data_attr="video_information",
        key_name="video_input",
        icon="mdi:video-input-component",
    ),
    DiagnosticSensorDescription(
        key="video_input_resolution",
        name="Video Input Resolution",
        data_attr="video_information",
        key_name="input_resolution",
        icon="mdi:aspect-ratio",
    ),
    DiagnosticSensorDescription(
        key="video_output",
        name="Video Output",
        data_attr="video_information",
        key_name="video_output",
        icon="mdi:video",
    ),
    DiagnosticSensorDescription(
        key="video_output_resolution",
        name="Video Output Resolution",
        data_attr="video_information",
        key_name="output_resolution",
        icon="mdi:monitor",
    ),
    DiagnosticSensorDescription(
        key="video_picture_mode",
        name="Video Picture Mode",
        data_attr="video_information",
        key_name="picture_mode",
        icon="mdi:image-filter-center-focus",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors from a config entry."""
    runtime: OnkyoRuntimeData = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for description in SENSOR_DESCRIPTIONS:
        command = "IFA" if description.data_attr == "audio_information" else "IFV"
        if runtime.coordinator.supports(command):
            entities.append(OnkyoLegacyDiagnosticSensor(runtime, description))

    if runtime.coordinator.supports("TUN"):
        entities.append(OnkyoLegacyTunerSensor(runtime))

    if entities:
        async_add_entities(entities)


class OnkyoLegacyDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """Represent a parsed audio/video diagnostic sensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        runtime: OnkyoRuntimeData,
        description: DiagnosticSensorDescription,
    ) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"{runtime.host}-{description.key}"
        self._attr_device_info = runtime.device_info
        self._attr_icon = description.icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def native_value(self) -> str | None:
        values = getattr(self.coordinator.data, self.entity_description.data_attr)
        return values.get(self.entity_description.key_name)

    @property
    def available(self) -> bool:
        command = "IFA" if self.entity_description.data_attr == "audio_information" else "IFV"
        return self.coordinator.last_update_success and self.coordinator.supports(command)


class OnkyoLegacyTunerSensor(CoordinatorEntity, SensorEntity):
    """Represent the current tuner frequency."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Tuner Frequency"
    _attr_icon = "mdi:radio"

    def __init__(self, runtime: OnkyoRuntimeData) -> None:
        super().__init__(runtime.coordinator)
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.host}-tuner-frequency"
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._runtime.entity_ids.add(self.entity_id)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.tuner_frequency

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.supports("TUN")
