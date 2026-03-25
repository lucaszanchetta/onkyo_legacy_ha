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

from .const import DEFAULT_MAX_VOLUME, SAFE_LISTENING_MODES

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


def _build_value_maps(command: str) -> tuple[dict[str, str], dict[str, str]]:
    """Build raw->alias and alias->raw maps for a main-zone command."""
    raw_to_name: dict[str, str] = {}
    name_to_raw: dict[str, str] = {}
    values = eiscp_commands.COMMANDS["main"][command]["values"]
    for raw, definition in values.items():
        alias = _primary_alias(definition["name"])
        raw_to_name[raw.upper()] = alias
        for item in _all_aliases(definition["name"]):
            name_to_raw[item.lower()] = raw.upper()
    return raw_to_name, name_to_raw


SOURCE_RAW_TO_NAME, SOURCE_NAME_TO_RAW = _build_value_maps("SLI")
LISTENING_RAW_TO_NAME, LISTENING_NAME_TO_RAW = _build_value_maps("LMD")
POWER_RAW_TO_NAME, _ = _build_value_maps("PWR")
MUTE_RAW_TO_NAME, _ = _build_value_maps("AMT")
SPEAKER_A_RAW_TO_NAME, _ = _build_value_maps("SPA")
SPEAKER_B_RAW_TO_NAME, _ = _build_value_maps("SPB")
TRIGGER_A_RAW_TO_NAME, _ = _build_value_maps("TGA")
TRIGGER_B_RAW_TO_NAME, _ = _build_value_maps("TGB")
TRIGGER_C_RAW_TO_NAME, _ = _build_value_maps("TGC")
DIMMER_RAW_TO_NAME, DIMMER_NAME_TO_RAW = _build_value_maps("DIM")


@dataclass(slots=True)
class OnkyoState:
    """Current device state."""

    power: bool = False
    volume: int = 0
    muted: bool = False
    source: str | None = None
    listening_mode: str | None = None
    speaker_a: bool | None = None
    speaker_b: bool | None = None
    trigger_a: bool | None = None
    trigger_b: bool | None = None
    trigger_c: bool | None = None
    dimmer_level: str | None = None


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
class OnkyoRuntimeData:
    """Runtime data shared across platforms."""

    title: str
    host: str
    port: int
    sources: dict[str, str]
    max_volume: int
    coordinator: "OnkyoLegacyCoordinator"
    source_lookup: dict[str, str] = field(default_factory=dict)
    supported_listening_modes: list[str] = field(default_factory=list)
    entity_ids: set[str] = field(default_factory=set)


class OnkyoLegacyCoordinator(DataUpdateCoordinator[OnkyoState]):
    """Centralized polling for a legacy Onkyo device."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client: OnkyoLegacyClient,
        host: str,
        name: str,
        update_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{name} coordinator",
            update_interval=None if update_interval <= 0 else timedelta(seconds=update_interval),
        )
        self.client = client
        self.host = host
        self.device_name = name
        self.supports_listening_mode = False
        self.supports_speaker_a = False
        self.supports_speaker_b = False
        self.supports_trigger_a = False
        self.supports_trigger_b = False
        self.supports_trigger_c = False
        self.supports_dimmer = False

    async def _async_update_data(self) -> OnkyoState:
        """Fetch the latest state from the device."""
        try:
            power_raw, volume_raw, mute_raw, source_raw = await self.hass.async_add_executor_job(
                self._query_core_state
            )
        except Exception as err:
            raise UpdateFailed(f"Core state refresh failed: {err}") from err

        listening_mode: str | None = self.data.listening_mode if self.data else None
        speaker_a: bool | None = self.data.speaker_a if self.data else None
        speaker_b: bool | None = self.data.speaker_b if self.data else None
        trigger_a: bool | None = self.data.trigger_a if self.data else None
        trigger_b: bool | None = self.data.trigger_b if self.data else None
        trigger_c: bool | None = self.data.trigger_c if self.data else None
        dimmer_level: str | None = self.data.dimmer_level if self.data else None

        if self.supports_listening_mode:
            listening_mode = await self._async_query_optional_mode("LMD")

        if self.supports_speaker_a:
            speaker_a = await self._async_query_optional_switch("SPA", SPEAKER_A_RAW_TO_NAME)

        if self.supports_speaker_b:
            speaker_b = await self._async_query_optional_switch("SPB", SPEAKER_B_RAW_TO_NAME)

        if self.supports_trigger_a:
            trigger_a = await self._async_query_optional_switch("TGA", TRIGGER_A_RAW_TO_NAME)

        if self.supports_trigger_b:
            trigger_b = await self._async_query_optional_switch("TGB", TRIGGER_B_RAW_TO_NAME)

        if self.supports_trigger_c:
            trigger_c = await self._async_query_optional_switch("TGC", TRIGGER_C_RAW_TO_NAME)

        if self.supports_dimmer:
            dimmer_level = await self._async_query_optional_mode("DIM")

        return OnkyoState(
            power=_parse_power(power_raw),
            volume=_parse_volume(volume_raw),
            muted=_parse_mute(mute_raw),
            source=_parse_enum(source_raw, SOURCE_RAW_TO_NAME),
            listening_mode=listening_mode,
            speaker_a=speaker_a,
            speaker_b=speaker_b,
            trigger_a=trigger_a,
            trigger_b=trigger_b,
            trigger_c=trigger_c,
            dimmer_level=dimmer_level,
        )

    def _query_core_state(self) -> tuple[str, str, str, str]:
        return (
            self.client.query("PWR"),
            self.client.query("MVL"),
            self.client.query("AMT"),
            self.client.query("SLI"),
        )

    async def _async_probe_optional(self, command: str) -> bool:
        try:
            await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.info("%s does not respond to %s queries: %s", self.host, command, err)
            return False
        return True

    async def _async_query_optional_mode(self, command: str) -> str | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_enum(raw, LISTENING_RAW_TO_NAME)

    async def _async_query_optional_switch(
        self, command: str, value_map: dict[str, str]
    ) -> bool | None:
        try:
            raw = await self.hass.async_add_executor_job(self.client.query, command)
        except Exception as err:
            _LOGGER.debug("Optional %s query failed for %s: %s", command, self.host, err)
            return None
        return _parse_switch(raw, value_map)

    async def async_turn_on(self) -> None:
        await self._async_send("PWR", "01")

    async def async_turn_off(self) -> None:
        await self._async_send("PWR", "00")

    async def async_set_muted(self, muted: bool) -> None:
        await self._async_send("AMT", "01" if muted else "00")

    async def async_volume_step(self, up: bool) -> None:
        await self._async_send("MVL", "UP1" if up else "DOWN1")

    async def async_set_volume(self, volume_level: float, max_volume: int = DEFAULT_MAX_VOLUME) -> None:
        target = max(0, min(round(volume_level * max_volume), max_volume))
        await self._async_send("MVL", f"{target:02X}")

    async def async_select_source(self, source_name: str) -> None:
        raw = SOURCE_NAME_TO_RAW[source_name.lower()]
        await self._async_send("SLI", raw)

    async def async_set_listening_mode(self, listening_mode: str) -> None:
        raw = LISTENING_NAME_TO_RAW[listening_mode.lower()]
        await self._async_send("LMD", raw)

    async def async_set_speaker(self, speaker: str, enabled: bool) -> None:
        await self._async_send(speaker, "01" if enabled else "00")

    async def async_set_trigger(self, trigger: str, enabled: bool) -> None:
        await self._async_send(trigger, "01" if enabled else "00")

    async def async_set_dimmer_level(self, dimmer_level: str) -> None:
        raw = DIMMER_NAME_TO_RAW[dimmer_level.lower()]
        await self._async_send("DIM", raw)

    async def _async_send(self, command: str, value: str) -> None:
        await self.hass.async_add_executor_job(self.client.send, command, value)
        await self.async_request_refresh()


def build_runtime_data(
    hass: HomeAssistant,
    *,
    host: str,
    port: int,
    name: str,
    scan_interval: int,
    sources: dict[str, str],
    max_volume: int,
) -> OnkyoRuntimeData:
    """Build runtime data for one configured device."""
    normalized_sources = _normalize_sources(sources)
    client = OnkyoLegacyClient(host, port)
    coordinator = OnkyoLegacyCoordinator(
        hass,
        client=client,
        host=host,
        name=name,
        update_interval=scan_interval,
    )
    source_lookup = {alias.lower(): label for label, alias in normalized_sources.items()}
    supported_modes = [mode for mode in SAFE_LISTENING_MODES if mode.lower() in LISTENING_NAME_TO_RAW]
    return OnkyoRuntimeData(
        title=name,
        host=host,
        port=port,
        sources=normalized_sources,
        max_volume=max_volume,
        coordinator=coordinator,
        source_lookup=source_lookup,
        supported_listening_modes=supported_modes,
    )


def _parse_power(response: str) -> bool:
    code = _payload_code(response, "PWR")
    value = POWER_RAW_TO_NAME.get(code)
    if value == "on":
        return True
    if value in {"standby", "off"}:
        return False
    raise ValueError(f"Unexpected power response: {response}")


def _parse_mute(response: str) -> bool:
    code = _payload_code(response, "AMT")
    value = MUTE_RAW_TO_NAME.get(code)
    if value == "on":
        return True
    if value == "off":
        return False
    raise ValueError(f"Unexpected mute response: {response}")


def _parse_volume(response: str) -> int:
    code = _payload_code(response, "MVL")
    return int(code, 16)


def _normalize_sources(sources: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for label, alias in sources.items():
        raw = alias.upper()
        if raw in SOURCE_RAW_TO_NAME:
            normalized[label] = SOURCE_RAW_TO_NAME[raw]
            continue

        normalized_alias = alias.lower()
        if normalized_alias not in SOURCE_NAME_TO_RAW:
            raise UpdateFailed(f"Unsupported source alias in configuration: {alias}")
        normalized[label] = normalized_alias
    return normalized


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


def _payload_code(response: str, expected_prefix: str) -> str:
    if not response.startswith(expected_prefix):
        raise ValueError(f"Unexpected response for {expected_prefix}: {response}")
    return response[len(expected_prefix) :].upper()
