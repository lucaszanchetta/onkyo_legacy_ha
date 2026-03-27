"""Coordinator and protocol wrapper for the Onkyo legacy integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from threading import Lock
from typing import Any

from eiscp import eISCP
from eiscp import commands as eiscp_commands
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_MAX_VOLUME,
    DEFAULT_MODEL,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    MODEL_TX8050,
    PROFILE_DEFAULT_SOURCES,
    PROFILE_QUERYABLE_COMMANDS,
    PROFILE_SUPPORTED_ZONES,
)

_LOGGER = logging.getLogger(__name__)


def _primary_alias(value: Any) -> str:
    """Return the primary alias for a command value."""
    if isinstance(value, tuple):
        return str(value[0])
    return str(value)


def _all_aliases(value: Any) -> list[str]:
    """Return all aliases for a command value."""
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return [str(value)]


def _build_value_maps(zone: str, command: str) -> tuple[dict[str, str], dict[str, str]]:
    """Build raw->alias and alias->raw maps for a command."""
    raw_to_name: dict[str, str] = {}
    name_to_raw: dict[str, str] = {}
    values = eiscp_commands.COMMANDS[zone][command]["values"]
    for raw, definition in values.items():
        alias = _primary_alias(definition["name"])
        raw_to_name[raw.upper()] = alias
        for item in _all_aliases(definition["name"]):
            name_to_raw[item.lower()] = raw.upper()
    return raw_to_name, name_to_raw


SOURCE_MAPS = {
    "main": _build_value_maps("main", "SLI"),
    "zone2": _build_value_maps("zone2", "SLZ"),
    "zone3": _build_value_maps("zone3", "SL3"),
}
LISTENING_RAW_TO_NAME, LISTENING_NAME_TO_RAW = _build_value_maps("main", "LMD")
LATE_NIGHT_RAW_TO_NAME, LATE_NIGHT_NAME_TO_RAW = _build_value_maps("main", "LTN")
POWER_RAW_TO_NAME, _ = _build_value_maps("main", "PWR")
MUTE_RAW_TO_NAME, _ = _build_value_maps("main", "AMT")
TRIGGER_A_RAW_TO_NAME, _ = _build_value_maps("main", "TGA")
TRIGGER_B_RAW_TO_NAME, _ = _build_value_maps("main", "TGB")
TRIGGER_C_RAW_TO_NAME, _ = _build_value_maps("main", "TGC")
DIMMER_RAW_TO_NAME, DIMMER_NAME_TO_RAW = _build_value_maps("main", "DIM")
AUDIO_SELECTOR_RAW_TO_NAME, AUDIO_SELECTOR_NAME_TO_RAW = _build_value_maps("main", "SLA")
CINEMA_FILTER_RAW_TO_NAME, _ = _build_value_maps("main", "RAS")
AUDYSSEY_EQ_RAW_TO_NAME, _ = _build_value_maps("main", "ADQ")
AUDYSSEY_VOLUME_RAW_TO_NAME, AUDYSSEY_VOLUME_NAME_TO_RAW = _build_value_maps("main", "ADV")
MUSIC_OPTIMIZER_RAW_TO_NAME, _ = _build_value_maps("main", "MOT")

SOURCE_DISPLAY_OVERRIDES = {
    "dvd": "DVD -- BD/DVD",
    "bd": "DVD -- BD/DVD",
    "tape-1": "TAPE -- TV/TAPE",
    "tv/tape": "TAPE -- TV/TAPE",
    "phono": "PHONO",
    "cd": "CD -- TV/CD",
    "tv/cd": "CD -- TV/CD",
    "fm": "FM",
    "am": "AM",
    "internet-radio": "INTERNET RADIO",
    "iradio-favorite": "INTERNET RADIO",
    "network": "NETWORK",
    "net": "NETWORK",
    "tv": "TV",
    "game": "GAME",
    "game/tv": "GAME",
    "pc": "PC",
    "usb": "USB",
}

OPTION_ALIAS_EXCLUSIONS: dict[str, set[str]] = {
    "LMD": {"up", "down", "movie", "music", "game", "thx", "auto", "surr", "stereo", "query"},
    "SLA": {"up", "query"},
    "LTN": {"up", "query"},
    "ADV": {"up", "query"},
    "DIM": {"query", "shut-off", "bright-led-off"},
}
OPTION_RAW_EXCLUSIONS: dict[str, set[str]] = {
    "LMD": {"UP", "DOWN", "MOVIE", "MUSIC", "GAME", "THX", "AUTO", "SURR", "STEREO", "QSTN"},
    "SLA": {"UP", "QSTN"},
    "LTN": {"UP", "QSTN"},
    "ADV": {"UP", "QSTN"},
    "DIM": {"DIM", "QSTN", "03", "08"},
}


@dataclass(frozen=True, slots=True)
class ZoneDefinition:
    """Core ISCP commands for one receiver zone."""

    key: str
    label: str
    eiscp_zone: str
    power_command: str
    volume_command: str
    mute_command: str
    source_command: str


ZONE_DEFINITIONS: dict[str, ZoneDefinition] = {
    "main": ZoneDefinition("main", "Main", "main", "PWR", "MVL", "AMT", "SLI"),
    "zone2": ZoneDefinition("zone2", "Zone 2", "zone2", "ZPW", "ZVL", "ZMT", "SLZ"),
    "zone3": ZoneDefinition("zone3", "Zone 3", "zone3", "PW3", "VL3", "MT3", "SL3"),
}


@dataclass(slots=True)
class OnkyoState:
    """Current device state."""

    power: bool = False
    volume: int | None = 0
    muted: bool | None = False
    source: str | None = None
    audio_selector: str | None = None
    listening_mode: str | None = None
    late_night_mode: str | None = None
    cinema_filter: bool | None = None
    audyssey_dynamic_eq: bool | None = None
    audyssey_dynamic_volume: str | None = None
    music_optimizer: bool | None = None
    trigger_a: bool | None = None
    trigger_b: bool | None = None
    trigger_c: bool | None = None
    dimmer_level: str | None = None
    sleep_minutes: int = 0
    center_level: int | None = None
    subwoofer_level: int | None = None
    tuner_frequency: int | None = None
    audio_information: dict[str, str] = field(default_factory=dict)
    video_information: dict[str, str] = field(default_factory=dict)


class OnkyoLegacyClient:
    """Thread-safe synchronous ISCP client."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._device: eISCP | None = None
        self._lock = Lock()

    def disconnect(self) -> None:
        """Disconnect the underlying socket."""
        with self._lock:
            if self._device is not None:
                try:
                    self._device.disconnect()
                except OSError:
                    pass
                self._device = None

    def query(self, command: str) -> str:
        """Query a command and return the raw response."""
        return self._with_retry(self._query_once, command)

    def send(self, command: str, value: str) -> None:
        """Send a raw command without waiting for an acknowledgement."""
        self._with_retry(self._send_once, command, value)

    def _connect(self) -> eISCP:
        if self._device is None:
            self._device = eISCP(self._host, self._port)
        return self._device

    def _query_once(self, command: str) -> str:
        with self._lock:
            return self._connect().raw(f"{command}QSTN")

    def _send_once(self, command: str, value: str) -> None:
        with self._lock:
            self._connect().send(f"{command}{value}")

    def _with_retry(self, method: Any, *args: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return method(*args)
            except Exception as err:  # broad to recover from socket/lib failures
                last_error = err
                _LOGGER.debug("ISCP command failed on attempt %s: %s", attempt + 1, err)
                self.disconnect()
        assert last_error is not None
        raise last_error


@dataclass(slots=True)
class OnkyoZoneRuntimeData:
    """Zone runtime data shared across platforms."""

    title: str
    zone_key: str
    zone_label: str
    host: str
    port: int
    sources: dict[str, str]
    max_volume: int
    coordinator: "OnkyoLegacyCoordinator"
    device_info: dict[str, Any]
    source_lookup: dict[str, str] = field(default_factory=dict)
    entity_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class OnkyoRuntimeData(OnkyoZoneRuntimeData):
    """Runtime data shared across platforms."""

    model: str = DEFAULT_MODEL
    queryable_commands: tuple[str, ...] = tuple()
    supported_listening_modes: list[str] = field(default_factory=list)
    supported_audio_selectors: list[str] = field(default_factory=list)
    supported_late_night_modes: list[str] = field(default_factory=list)
    supported_audyssey_volume_modes: list[str] = field(default_factory=list)
    supported_dimmer_modes: list[str] = field(default_factory=list)
    zones: tuple[OnkyoZoneRuntimeData, ...] = field(default_factory=tuple)
    candidate_zones: tuple[OnkyoZoneRuntimeData, ...] = field(default_factory=tuple)


class OnkyoLegacyCoordinator(DataUpdateCoordinator[OnkyoState]):
    """Centralized polling for one legacy Onkyo zone."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client: OnkyoLegacyClient,
        host: str,
        name: str,
        zone: ZoneDefinition | None = None,
        update_interval: int,
        source_raw_to_name: dict[str, str] | None = None,
        source_name_to_raw: dict[str, str] | None = None,
    ) -> None:
        zone = zone or ZONE_DEFINITIONS["main"]
        source_raw_to_name = source_raw_to_name or SOURCE_MAPS[zone.key][0]
        source_name_to_raw = source_name_to_raw or SOURCE_MAPS[zone.key][1]
        super().__init__(
            hass,
            _LOGGER,
            name=f"{name} {zone.label.lower()} coordinator",
            update_interval=None if update_interval <= 0 else timedelta(seconds=update_interval),
        )
        self.client = client
        self.host = host
        self.device_name = name
        self.zone = zone
        self.source_raw_to_name = source_raw_to_name
        self.source_name_to_raw = source_name_to_raw
        self.command_capabilities: dict[str, bool] = {}

    def set_command_capability(self, command: str, supported: bool) -> None:
        self.command_capabilities[command] = supported

    def supports(self, command: str) -> bool:
        return self.command_capabilities.get(command, False)

    async def async_probe_command(self, command: str) -> bool:
        return await self._async_probe_optional(command)

    async def _async_update_data(self) -> OnkyoState:
        """Fetch the latest state from the device."""
        try:
            power_raw, volume_raw, mute_raw, source_raw = await self.hass.async_add_executor_job(
                self._query_core_state
            )
        except Exception as err:
            raise UpdateFailed(f"Core state refresh failed: {err}") from err

        state = OnkyoState(
            power=_parse_power(power_raw, self.zone.power_command),
            volume=_parse_volume(volume_raw, self.zone.volume_command),
            muted=_parse_mute(mute_raw, self.zone.mute_command),
            source=_parse_enum(source_raw, self.source_raw_to_name),
        )
        if self.zone.key != "main":
            return state

        previous = self.data or OnkyoState()

        if self.supports("SLA"):
            state.audio_selector = await self._async_query_optional_mode(
                "SLA", AUDIO_SELECTOR_RAW_TO_NAME
            )
        else:
            state.audio_selector = previous.audio_selector

        if self.supports("LMD"):
            state.listening_mode = await self._async_query_optional_mode("LMD")
        else:
            state.listening_mode = previous.listening_mode

        if self.supports("LTN"):
            state.late_night_mode = await self._async_query_optional_mode(
                "LTN", LATE_NIGHT_RAW_TO_NAME
            )
        else:
            state.late_night_mode = previous.late_night_mode

        if self.supports("RAS"):
            state.cinema_filter = await self._async_query_optional_switch(
                "RAS", CINEMA_FILTER_RAW_TO_NAME
            )
        else:
            state.cinema_filter = previous.cinema_filter

        if self.supports("ADQ"):
            state.audyssey_dynamic_eq = await self._async_query_optional_switch(
                "ADQ", AUDYSSEY_EQ_RAW_TO_NAME
            )
        else:
            state.audyssey_dynamic_eq = previous.audyssey_dynamic_eq

        if self.supports("ADV"):
            state.audyssey_dynamic_volume = await self._async_query_optional_mode(
                "ADV", AUDYSSEY_VOLUME_RAW_TO_NAME
            )
        else:
            state.audyssey_dynamic_volume = previous.audyssey_dynamic_volume

        if self.supports("MOT"):
            state.music_optimizer = await self._async_query_optional_switch(
                "MOT", MUSIC_OPTIMIZER_RAW_TO_NAME
            )
        else:
            state.music_optimizer = previous.music_optimizer

        if self.supports("TGA"):
            state.trigger_a = await self._async_query_optional_switch("TGA", TRIGGER_A_RAW_TO_NAME)
        else:
            state.trigger_a = previous.trigger_a

        if self.supports("TGB"):
            state.trigger_b = await self._async_query_optional_switch("TGB", TRIGGER_B_RAW_TO_NAME)
        else:
            state.trigger_b = previous.trigger_b

        if self.supports("TGC"):
            state.trigger_c = await self._async_query_optional_switch("TGC", TRIGGER_C_RAW_TO_NAME)
        else:
            state.trigger_c = previous.trigger_c

        if self.supports("DIM"):
            state.dimmer_level = await self._async_query_optional_mode("DIM", DIMMER_RAW_TO_NAME)
        else:
            state.dimmer_level = previous.dimmer_level

        if self.supports("SLP"):
            state.sleep_minutes = await self._async_query_optional_sleep("SLP")
        else:
            state.sleep_minutes = previous.sleep_minutes

        if self.supports("CTL"):
            state.center_level = await self._async_query_optional_level("CTL")
        else:
            state.center_level = previous.center_level

        if self.supports("SWL"):
            state.subwoofer_level = await self._async_query_optional_level("SWL")
        else:
            state.subwoofer_level = previous.subwoofer_level

        if self.supports("TUN"):
            state.tuner_frequency = await self._async_query_optional_tuner("TUN")
        else:
            state.tuner_frequency = previous.tuner_frequency

        if self.supports("IFA"):
            state.audio_information = await self._async_query_optional_information(
                "IFA", _parse_audio_information
            )
        else:
            state.audio_information = dict(previous.audio_information)

        if self.supports("IFV"):
            state.video_information = await self._async_query_optional_information(
                "IFV", _parse_video_information
            )
        else:
            state.video_information = dict(previous.video_information)

        state.listening_mode = _resolve_listening_mode_display(
            state.listening_mode, state.audio_information
        )
        return state

    def _query_core_state(self) -> tuple[str, str, str, str]:
        return (
            self.client.query(self.zone.power_command),
            self.client.query(self.zone.volume_command),
            self.client.query(self.zone.mute_command),
            self.client.query(self.zone.source_command),
        )

    async def _async_probe_optional(self, command: str) -> bool:
        try:
            await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.info("%s does not respond to %s queries: %s", self.host, command, err)
            return False
        return True

    async def _async_query_optional_mode(
        self, command: str, value_map: dict[str, str] | None = None
    ) -> str | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_enum(raw, value_map or LISTENING_RAW_TO_NAME)

    async def _async_query_optional_switch(
        self, command: str, value_map: dict[str, str]
    ) -> bool | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_switch(raw, value_map)

    async def _async_query_optional_sleep(self, command: str) -> int:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return 0
        return _parse_sleep(raw)

    async def _async_query_optional_level(self, command: str) -> int | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_signed_level(raw)

    async def _async_query_optional_tuner(self, command: str) -> int | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_tuner_frequency(raw)

    async def _async_query_optional_information(
        self,
        command: str,
        parser: Any,
    ) -> dict[str, str]:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return {}
        return parser(raw)

    async def async_turn_on(self) -> None:
        await self._async_send(self.zone.power_command, "01")

    async def async_turn_off(self) -> None:
        await self._async_send(self.zone.power_command, "00")

    async def async_set_muted(self, muted: bool) -> None:
        await self._async_send(self.zone.mute_command, "01" if muted else "00")

    async def async_volume_step(self, up: bool) -> None:
        await self._async_send(self.zone.volume_command, "UP1" if up else "DOWN1")

    async def async_set_volume(self, volume_level: float, max_volume: int = DEFAULT_MAX_VOLUME) -> None:
        target = max(0, min(round(volume_level * max_volume), max_volume))
        await self._async_send(self.zone.volume_command, f"{target:02X}")

    async def async_select_source(self, source_name: str) -> None:
        raw = self.source_name_to_raw[source_name.lower()]
        await self._async_send(self.zone.source_command, raw)

    async def async_set_audio_selector(self, selector: str) -> None:
        raw = AUDIO_SELECTOR_NAME_TO_RAW[selector.lower()]
        await self._async_send("SLA", raw)

    async def async_set_listening_mode(self, listening_mode: str) -> None:
        raw = LISTENING_NAME_TO_RAW[listening_mode.lower()]
        await self._async_send("LMD", raw)

    async def async_set_late_night_mode(self, late_night_mode: str) -> None:
        raw = LATE_NIGHT_NAME_TO_RAW[late_night_mode.lower()]
        await self._async_send("LTN", raw)

    async def async_set_trigger(self, trigger: str, enabled: bool) -> None:
        await self._async_send(trigger, "01" if enabled else "00")

    async def async_set_dimmer_level(self, dimmer_level: str) -> None:
        raw = DIMMER_NAME_TO_RAW[dimmer_level.lower()]
        await self._async_send("DIM", raw)

    async def async_set_audyssey_dynamic_volume(self, option: str) -> None:
        raw = AUDYSSEY_VOLUME_NAME_TO_RAW[option.lower()]
        await self._async_send("ADV", raw)

    async def async_set_sleep_minutes(self, minutes: int) -> None:
        raw = "OFF" if minutes <= 0 else f"{minutes:02X}"
        await self._async_send("SLP", raw)

    async def async_set_level(self, command: str, level: int) -> None:
        raw = _encode_signed_level(level)
        await self._async_send(command, raw)

    async def async_set_boolean_option(self, command: str, enabled: bool) -> None:
        await self._async_send(command, "01" if enabled else "00")

    async def _async_send(self, command: str, value: str) -> None:
        await self.hass.async_add_executor_job(self.client.send, command, value)
        await self.async_request_refresh()


def build_runtime_data(
    hass: HomeAssistant,
    *,
    host: str,
    port: int,
    name: str,
    model: str = DEFAULT_MODEL,
    scan_interval: int,
    sources: dict[str, str],
    max_volume: int,
) -> OnkyoRuntimeData:
    """Build runtime data for one configured device."""
    normalized_model = _normalize_model(model)
    normalized_sources = _normalize_sources(sources or PROFILE_DEFAULT_SOURCES[normalized_model])
    client = OnkyoLegacyClient(host, port)
    entity_ids: set[str] = set()
    device_info = {
        "identifiers": {(DOMAIN, host)},
        "name": name,
        "manufacturer": MANUFACTURER,
        "model": normalized_model,
        "configuration_url": f"http://{host}",
    }

    main_raw_to_name, main_name_to_raw = SOURCE_MAPS["main"]
    coordinator = OnkyoLegacyCoordinator(
        hass,
        client=client,
        host=host,
        name=name,
        zone=ZONE_DEFINITIONS["main"],
        update_interval=scan_interval,
        source_raw_to_name=main_raw_to_name,
        source_name_to_raw=main_name_to_raw,
    )
    source_lookup = _build_source_lookup(normalized_sources, main_raw_to_name, main_name_to_raw)
    runtime = OnkyoRuntimeData(
        title=name,
        zone_key="main",
        zone_label="Main",
        model=normalized_model,
        host=host,
        port=port,
        sources=normalized_sources,
        max_volume=max_volume,
        coordinator=coordinator,
        device_info=device_info,
        queryable_commands=PROFILE_QUERYABLE_COMMANDS[normalized_model],
        source_lookup=source_lookup,
        supported_listening_modes=_build_select_options("LMD"),
        supported_audio_selectors=_build_select_options("SLA") if normalized_model == MODEL else [],
        supported_late_night_modes=_build_select_options("LTN") if normalized_model == MODEL else [],
        supported_audyssey_volume_modes=_build_select_options("ADV") if normalized_model == MODEL else [],
        supported_dimmer_modes=_build_select_options("DIM"),
        entity_ids=entity_ids,
    )

    candidate_zone_keys = PROFILE_SUPPORTED_ZONES[normalized_model]
    candidate_zones = tuple(
        _build_zone_runtime(
            hass=hass,
            host=host,
            port=port,
            name=name,
            scan_interval=scan_interval,
            max_volume=max_volume,
            configured_sources=normalized_sources,
            device_info=device_info,
            entity_ids=entity_ids,
            client=client,
            zone=ZONE_DEFINITIONS[zone_key],
        )
        for zone_key in candidate_zone_keys
        if zone_key != "main"
    )
    runtime.zones = (runtime,)
    runtime.candidate_zones = candidate_zones
    return runtime


def _build_zone_runtime(
    *,
    hass: HomeAssistant,
    host: str,
    port: int,
    name: str,
    scan_interval: int,
    max_volume: int,
    configured_sources: dict[str, str],
    device_info: dict[str, Any],
    entity_ids: set[str],
    client: OnkyoLegacyClient,
    zone: ZoneDefinition,
) -> OnkyoZoneRuntimeData:
    raw_to_name, name_to_raw = SOURCE_MAPS[zone.key]
    zone_sources = _filter_zone_sources(configured_sources, name_to_raw, raw_to_name)
    coordinator = OnkyoLegacyCoordinator(
        hass,
        client=client,
        host=host,
        name=name,
        zone=zone,
        update_interval=scan_interval,
        source_raw_to_name=raw_to_name,
        source_name_to_raw=name_to_raw,
    )
    return OnkyoZoneRuntimeData(
        title=name,
        zone_key=zone.key,
        zone_label=zone.label,
        host=host,
        port=port,
        sources=zone_sources,
        max_volume=_zone_max_volume(zone.key, max_volume),
        coordinator=coordinator,
        device_info=device_info,
        source_lookup=_build_source_lookup(zone_sources, raw_to_name, name_to_raw),
        entity_ids=entity_ids,
    )


def _parse_power(response: str, command: str) -> bool:
    code = _payload_code(response, command)
    value = POWER_RAW_TO_NAME.get(code)
    if value == "on":
        return True
    if value in {"standby", "off"}:
        return False
    raise ValueError(f"Unexpected power response: {response}")


def _parse_mute(response: str, command: str) -> bool | None:
    code = _payload_code(response, command)
    if code == "N/A":
        return None
    value = MUTE_RAW_TO_NAME.get(code)
    if value == "on":
        return True
    if value == "off":
        return False
    raise ValueError(f"Unexpected mute response: {response}")


def _parse_volume(response: str, command: str) -> int | None:
    code = _payload_code(response, command)
    if code == "N/A":
        return None
    return int(code, 16)


def _parse_sleep(response: str) -> int:
    code = _payload_code(response, "SLP")
    if code == "OFF" or code == "00":
        return 0
    return int(code, 16)


def _parse_signed_level(response: str) -> int:
    code = response[3:]
    if not code:
        raise ValueError(f"Unexpected signed level response: {response}")
    if code == "00":
        return 0
    return int(code)


def _parse_tuner_frequency(response: str) -> int:
    code = _payload_code(response, "TUN")
    if not code:
        raise ValueError(f"Unexpected tuner response: {response}")
    return int(code)


def _normalize_sources(sources: dict[str, str]) -> dict[str, str]:
    source_raw_to_name, source_name_to_raw = SOURCE_MAPS["main"]
    normalized: dict[str, str] = {}
    for label, alias in sources.items():
        raw = alias.upper()
        if raw in source_raw_to_name:
            canonical_alias = source_raw_to_name[raw]
            normalized[_display_source_name(canonical_alias)] = canonical_alias
            continue

        normalized_alias = alias.lower()
        if normalized_alias not in source_name_to_raw:
            raise UpdateFailed(f"Unsupported source alias in configuration: {alias}")
        canonical_alias = source_raw_to_name[source_name_to_raw[normalized_alias]]
        normalized[_display_source_name(canonical_alias)] = canonical_alias
    return normalized


def _build_source_lookup(
    sources: dict[str, str],
    raw_to_name: dict[str, str],
    name_to_raw: dict[str, str],
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for display_name, alias in sources.items():
        lookup[alias.lower()] = display_name
        raw = name_to_raw.get(alias.lower())
        if raw is None:
            continue
        for candidate_alias, candidate_raw in name_to_raw.items():
            if candidate_raw == raw:
                lookup[candidate_alias.lower()] = display_name
        canonical_alias = raw_to_name.get(raw)
        if canonical_alias is not None:
            lookup[canonical_alias.lower()] = display_name
    return lookup


def _filter_zone_sources(
    configured_sources: dict[str, str],
    zone_name_to_raw: dict[str, str],
    zone_raw_to_name: dict[str, str],
) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for label, alias in configured_sources.items():
        raw = zone_name_to_raw.get(alias.lower())
        if raw is None:
            continue
        filtered[label] = zone_raw_to_name[raw]
    return filtered


def _normalize_model(model: str) -> str:
    candidate = model.strip().upper()
    if candidate == MODEL:
        return MODEL
    if candidate == MODEL_TX8050:
        return MODEL_TX8050
    raise UpdateFailed(f"Unsupported Onkyo model: {model}")


def _display_source_name(alias: str) -> str:
    override = SOURCE_DISPLAY_OVERRIDES.get(alias.lower())
    if override:
        return override
    return alias.replace("-", " ").upper()


def _zone_max_volume(zone_key: str, configured_max_volume: int) -> int:
    if zone_key in {"zone2", "zone3"}:
        return max(configured_max_volume, 100)
    return configured_max_volume


def _parse_enum(response: str, value_map: dict[str, str]) -> str | None:
    prefix = response[:3]
    code = _payload_code(response, prefix)
    return value_map.get(code)


def _parse_switch(response: str, value_map: dict[str, str]) -> bool | None:
    prefix = response[:3]
    code = _payload_code(response, prefix)
    value = value_map.get(code)
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _parse_audio_information(response: str) -> dict[str, str]:
    payload = _payload_code(response, "IFA")
    return _split_information(
        payload,
        (
            "input_terminal",
            "input_signal",
            "sampling_frequency",
            "input_channels",
            "listening_mode",
            "output_channels",
            "output_frequency",
        ),
    )


def _parse_video_information(response: str) -> dict[str, str]:
    payload = _payload_code(response, "IFV")
    return _split_information(
        payload,
        (
            "video_input",
            "input_resolution",
            "input_color_format",
            "input_color_depth",
            "video_output",
            "output_resolution",
            "output_color_format",
            "output_color_depth",
            "picture_mode",
        ),
    )


def _split_information(payload: str, fields: tuple[str, ...]) -> dict[str, str]:
    parts = [part.strip() for part in payload.split(",")]
    result: dict[str, str] = {}
    for index, field in enumerate(fields):
        if index >= len(parts):
            break
        value = parts[index]
        if value:
            result[field] = value
    return result


def _build_select_options(command: str) -> list[str]:
    values = eiscp_commands.COMMANDS["main"][command]["values"]
    raw_exclusions = OPTION_RAW_EXCLUSIONS.get(command, set())
    alias_exclusions = OPTION_ALIAS_EXCLUSIONS.get(command, set())
    options: list[str] = []
    seen: set[str] = set()
    for raw, definition in values.items():
        raw_key = raw.upper()
        if raw_key in raw_exclusions:
            continue
        alias = _primary_alias(definition["name"]).lower()
        if alias in alias_exclusions or alias in seen:
            continue
        seen.add(alias)
        options.append(alias)
    return options


def _resolve_listening_mode_display(
    listening_mode: str | None,
    audio_information: dict[str, str],
) -> str | None:
    audio_mode = audio_information.get("listening_mode")
    if audio_mode:
        return audio_mode.upper()
    return listening_mode


def _payload_code(response: str, expected_prefix: str) -> str:
    if not response.startswith(expected_prefix):
        raise ValueError(f"Unexpected response for {expected_prefix}: {response}")
    return response[len(expected_prefix) :].upper()


def _encode_signed_level(level: int) -> str:
    if level == 0:
        return "00"
    return str(level)
