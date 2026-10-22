"""Coordinator and protocol wrapper for the Onkyo legacy integration."""

from __future__ import annotations

import asyncio
import logging
import select
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any, Callable

from eiscp import commands as eiscp_commands
from eiscp import eISCP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_MAX_VOLUME,
    DEFAULT_MODEL,
    DOMAIN,
    GENERIC_PROFILE,
    MANUFACTURER,
    MODEL,
    MODEL_TX8050,
    PROFILES,
    ModelProfile,
)

__all__ = [
    "OnkyoLegacyClient",
    "OnkyoLegacyCoordinator",
    "OnkyoRuntimeData",
    "OnkyoZoneRuntimeData",
    "OnkyoState",
    "ZoneDefinition",
    "ZONE_DEFINITIONS",
    "SOURCE_MAPS",
    "build_runtime_data",
]

_LOGGER = logging.getLogger(__name__)


class _LatencySummary:
    """Fixed-size latency summary with no retained sample history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.last_ms: float | None = None

    def record(self, elapsed_ms: float) -> None:
        value = max(0.0, elapsed_ms)
        with self._lock:
            self.count += 1
            self.total_ms += value
            self.max_ms = max(self.max_ms, value)
            self.last_ms = value

    def snapshot(self) -> dict[str, int | float | None]:
        with self._lock:
            return {
                "count": self.count,
                "total_ms": round(self.total_ms, 3),
                "max_ms": round(self.max_ms, 3),
                "last_ms": round(self.last_ms, 3) if self.last_ms is not None else None,
            }


class _ClientMetrics:
    """Bounded client metrics using only fixed categories and counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queue_latency = {
            "control": _LatencySummary(),
            "core": _LatencySummary(),
            "background": _LatencySummary(),
        }
        self.response_latency = {
            "core": _LatencySummary(),
            "optional": _LatencySummary(),
        }
        self.connect_attempts = 0
        self.reconnect_attempts = 0
        self.optional_timeouts = 0
        self._successful_connections = 0
        self._last_valid_frame_at: float | None = None
        self._last_matched_response_at: float | None = None

    def record_queue(self, priority: int, elapsed_ms: float) -> None:
        category = "control" if priority == 2 else "core" if priority == 1 else "background"
        self.queue_latency[category].record(elapsed_ms)

    def record_response(self, category: str, elapsed_ms: float) -> None:
        self.response_latency[category].record(elapsed_ms)
        now = time.monotonic()
        with self._lock:
            self._last_matched_response_at = now

    def record_valid_frame(self) -> None:
        with self._lock:
            self._last_valid_frame_at = time.monotonic()

    def record_connect_attempt(self) -> None:
        with self._lock:
            self.connect_attempts += 1
            if self._successful_connections:
                self.reconnect_attempts += 1

    def record_connection_success(self) -> None:
        with self._lock:
            self._successful_connections += 1

    def record_optional_timeout(self) -> None:
        with self._lock:
            self.optional_timeouts += 1

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            frame_age = (
                max(0.0, now - self._last_valid_frame_at)
                if self._last_valid_frame_at is not None
                else None
            )
            matched_age = (
                max(0.0, now - self._last_matched_response_at)
                if self._last_matched_response_at is not None
                else None
            )
            counters = {
                "connect_attempts": self.connect_attempts,
                "reconnect_attempts": self.reconnect_attempts,
                "optional_timeouts": self.optional_timeouts,
            }
        return {
            **counters,
            "queue_latency_ms": {
                key: metric.snapshot() for key, metric in self.queue_latency.items()
            },
            "matched_response_latency_ms": {
                key: metric.snapshot() for key, metric in self.response_latency.items()
            },
            "last_valid_frame_age_seconds": round(frame_age, 3) if frame_age is not None else None,
            "last_matched_response_age_seconds": round(matched_age, 3) if matched_age is not None else None,
        }


class CommandResponseTimeout(TimeoutError):
    """A complete query send received no matching response before timeout."""


class _OperationGate:
    """Serialize socket operations while giving queued controls bounded priority."""

    def __init__(self, metrics: _ClientMetrics | None = None) -> None:
        self._condition = threading.Condition()
        self._waiters: deque[tuple[int, object]] = deque()
        self._active = False
        self._high_streak = 0
        self._core_streak = 0
        self._metrics = metrics

    def acquire(self, priority: int) -> None:
        started = time.perf_counter()
        token = object()
        with self._condition:
            self._waiters.append((priority, token))
            while True:
                controls = [item for item in self._waiters if item[0] == 2]
                core = [item for item in self._waiters if item[0] == 1]
                background = [item for item in self._waiters if item[0] == 0]
                lower = core + background
                if not self._active:
                    if controls and (self._high_streak < 4 or not lower):
                        selected = controls[0]
                    elif core and (self._core_streak < 4 or not background):
                        selected = core[0]
                    elif background:
                        selected = background[0]
                    elif core:
                        selected = core[0]
                    elif controls:
                        selected = controls[0]
                    else:
                        selected = None
                    if selected is not None and selected[1] is token:
                        self._waiters.remove(selected)
                        self._active = True
                        self._high_streak = (
                            self._high_streak + 1 if priority == 2 else 0
                        )
                        if priority == 1:
                            self._core_streak += 1
                        elif priority == 0:
                            self._core_streak = 0
                        if self._metrics is not None:
                            self._metrics.record_queue(
                                priority, (time.perf_counter() - started) * 1000
                            )
                        return
                self._condition.wait()

    def release(self) -> None:
        with self._condition:
            self._active = False
            self._condition.notify_all()

    def __enter__(self) -> _OperationGate:
        self.acquire(0)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class _VolumeWrite:
    """Mutable absolute target that becomes immutable at socket-gate grant."""

    def __init__(self, value: str, revision: Callable[[], int]) -> None:
        self._lock = threading.Lock()
        self._value = value
        self._revision = revision
        self.claimed = False
        self.start_revision = 0
        self.claimed_value: str | None = None

    def update(self, value: str) -> bool:
        with self._lock:
            if self.claimed:
                return False
            self._value = value
            return True

    def claim(self) -> str:
        with self._lock:
            self.claimed = True
            self.start_revision = self._revision()
            self.claimed_value = self._value
            return self._value


@dataclass
class _WriteIntent:
    command: str
    value: str
    future: asyncio.Future[None]
    volume_write: _VolumeWrite | None = None
    absolute_target: int | None = None
    waiters: list[asyncio.Future[None]] = field(default_factory=list)
    sequence: int = 0
    push_event: asyncio.Event | None = None

    def __post_init__(self) -> None:
        if not self.waiters:
            self.waiters.append(self.future)


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
AUDIO_SELECTOR_RAW_TO_NAME, AUDIO_SELECTOR_NAME_TO_RAW = _build_value_maps(
    "main", "SLA"
)
CINEMA_FILTER_RAW_TO_NAME, _ = _build_value_maps("main", "RAS")
AUDYSSEY_EQ_RAW_TO_NAME, _ = _build_value_maps("main", "ADQ")
AUDYSSEY_VOLUME_RAW_TO_NAME, AUDYSSEY_VOLUME_NAME_TO_RAW = _build_value_maps(
    "main", "ADV"
)
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
    "LMD": {
        "up",
        "down",
        "movie",
        "music",
        "game",
        "thx",
        "auto",
        "surr",
        "stereo",
        "query",
    },
    "SLA": {"up", "query"},
    "LTN": {"up", "query"},
    "ADV": {"up", "query"},
    "DIM": {"query", "shut-off", "bright-led-off"},
}
OPTION_RAW_EXCLUSIONS: dict[str, set[str]] = {
    "LMD": {
        "UP",
        "DOWN",
        "MOVIE",
        "MUSIC",
        "GAME",
        "THX",
        "AUTO",
        "SURR",
        "STEREO",
        "QSTN",
    },
    "SLA": {"UP", "QSTN"},
    "LTN": {"UP", "QSTN"},
    "ADV": {"UP", "QSTN"},
    "DIM": {"DIM", "QSTN", "03", "08"},
}

_OPTIONAL_ATTR_MAP: dict[str, str] = {
    "SLA": "audio_selector",
    "LMD": "listening_mode",
    "LTN": "late_night_mode",
    "RAS": "cinema_filter",
    "ADQ": "audyssey_dynamic_eq",
    "ADV": "audyssey_dynamic_volume",
    "MOT": "music_optimizer",
    "TGA": "trigger_a",
    "TGB": "trigger_b",
    "TGC": "trigger_c",
    "DIM": "dimmer_level",
    "CTL": "center_level",
    "SWL": "subwoofer_level",
    "IFA": "audio_information",
    "IFV": "video_information",
    "RES": "video_information",
    "HDO": "video_information",
}

_OPTIONAL_MODE_COMMANDS: tuple[tuple[str, dict[str, str] | None], ...] = (
    ("SLA", AUDIO_SELECTOR_RAW_TO_NAME),
    ("LMD", None),
    ("LTN", LATE_NIGHT_RAW_TO_NAME),
    ("DIM", DIMMER_RAW_TO_NAME),
    ("ADV", AUDYSSEY_VOLUME_RAW_TO_NAME),
)
_OPTIONAL_SWITCH_COMMANDS: tuple[tuple[str, dict[str, str]], ...] = (
    ("RAS", CINEMA_FILTER_RAW_TO_NAME),
    ("ADQ", AUDYSSEY_EQ_RAW_TO_NAME),
    ("MOT", MUSIC_OPTIMIZER_RAW_TO_NAME),
    ("TGA", TRIGGER_A_RAW_TO_NAME),
    ("TGB", TRIGGER_B_RAW_TO_NAME),
    ("TGC", TRIGGER_C_RAW_TO_NAME),
)
_OPTIONAL_SLEEP_COMMANDS: tuple[str, ...] = ("SLP",)
_OPTIONAL_LEVEL_COMMANDS: tuple[str, ...] = ("CTL", "SWL")
_OPTIONAL_TUNER_COMMANDS: tuple[str, ...] = ("TUN",)
_OPTIONAL_INFORMATION_COMMANDS: tuple[str, ...] = ("IFA", "IFV", "RES", "HDO")
_SINGLE_VALUE_INFORMATION: tuple[str, ...] = ("RES", "HDO")


def _apply_previous_fallbacks(state: OnkyoState, previous: OnkyoState) -> OnkyoState:
    """Fill in None/empty optional fields from the previous state."""
    if state.audio_selector is None:
        state.audio_selector = previous.audio_selector
    if state.listening_mode is None:
        state.listening_mode = previous.listening_mode
    if state.late_night_mode is None:
        state.late_night_mode = previous.late_night_mode
    if state.cinema_filter is None:
        state.cinema_filter = previous.cinema_filter
    if state.audyssey_dynamic_eq is None:
        state.audyssey_dynamic_eq = previous.audyssey_dynamic_eq
    if state.audyssey_dynamic_volume is None:
        state.audyssey_dynamic_volume = previous.audyssey_dynamic_volume
    if state.music_optimizer is None:
        state.music_optimizer = previous.music_optimizer
    if state.trigger_a is None:
        state.trigger_a = previous.trigger_a
    if state.trigger_b is None:
        state.trigger_b = previous.trigger_b
    if state.trigger_c is None:
        state.trigger_c = previous.trigger_c
    if state.dimmer_level is None:
        state.dimmer_level = previous.dimmer_level
    if state.sleep_minutes is None:
        state.sleep_minutes = previous.sleep_minutes
    if state.center_level is None:
        state.center_level = previous.center_level
    if state.subwoofer_level is None:
        state.subwoofer_level = previous.subwoofer_level
    if state.tuner_frequency is None:
        state.tuner_frequency = previous.tuner_frequency
    if not state.audio_information:
        state.audio_information = dict(previous.audio_information)
    if not state.video_information:
        state.video_information = dict(previous.video_information)
    return state


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
    optional_commands: tuple[str, ...] = ()


ZONE_DEFINITIONS: dict[str, ZoneDefinition] = {
    "main": ZoneDefinition("main", "Main", "main", "PWR", "MVL", "AMT", "SLI"),
    "zone2": ZoneDefinition(
        "zone2",
        "Zone 2",
        "zone2",
        "ZPW",
        "ZVL",
        "ZMT",
        "SLZ",
        optional_commands=("LMD",),
    ),
    "zone3": ZoneDefinition(
        "zone3",
        "Zone 3",
        "zone3",
        "PW3",
        "VL3",
        "MT3",
        "SL3",
        optional_commands=("LMD",),
    ),
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
    sleep_minutes: int | None = None
    center_level: int | None = None
    subwoofer_level: int | None = None
    tuner_frequency: int | None = None
    audio_information: dict[str, str] = field(default_factory=dict)
    video_information: dict[str, str] = field(default_factory=dict)


class OnkyoLegacyClient:
    """Thread-safe synchronous ISCP client."""

    _RETRYABLE_ERRORS = (OSError, TimeoutError, ConnectionError)
    _COOLDOWN_BASE = 2.0
    _COOLDOWN_MAX = 60.0
    _COOLDOWN_MAX_EXPONENT = 5
    _CONNECT_TIMEOUT = 5.0

    def __init__(self, host: str, port: int, retries: int = 2) -> None:
        self._host = host
        self._port = port
        self._device: eISCP | None = None
        self._lock = Lock()
        self._retries = max(1, retries)
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0
        self._listener_thread: threading.Thread | None = None
        self._listener_stop: threading.Event = threading.Event()
        self._push_callback: Callable[[str], None] | None = None
        self._closed = False
        self.metrics = _ClientMetrics()
        self._operation_lock = _OperationGate(self.metrics)
        self._query_kind = "core"

    def disconnect(self) -> None:
        """Reset the socket while leaving the listener available."""
        self._reset_socket()

    def _reset_socket(self) -> None:
        with self._lock:
            if self._device is not None:
                try:
                    self._device.disconnect()
                except OSError:
                    pass
                self._device = None

    def close(self) -> None:
        """Stop the listener and release the socket permanently."""
        self.begin_close()
        self.stop_listener()
        self._reset_socket()

    def begin_close(self) -> None:
        """Prevent queued operations from reconnecting during shutdown."""
        self._closed = True
        self._listener_stop.set()

    def query(self, command: str) -> str:
        """Query a command and return the raw response."""
        return self._with_retry(self._query_once, command, priority=False)

    def query_optional(self, command: str, timeout: float = 1.5) -> str:
        """Query an optional command once without delaying core polling."""
        self._operation_lock.acquire(0)
        try:
            if self._closed:
                raise ConnectionError("Onkyo client is closed")
            if self._cooldown_remaining():
                raise ConnectionError(
                    f"Receiver cooldown active for {self._host}:{self._port}"
                )
            self._query_kind = "optional"
            try:
                return self._query_once(command, timeout=timeout)
            except CommandResponseTimeout:
                self.metrics.record_optional_timeout()
                raise
            except (OSError, TimeoutError, ConnectionError, ValueError):
                self._record_failure()
                self._reset_socket()
                raise
        finally:
            self._query_kind = "core"
            self._operation_lock.release()

    def query_batch(self, commands: tuple[str, ...]) -> dict[str, str]:
        """Query commands in order, yielding the socket gate between responses."""
        return {
            command: self._with_retry(self._query_once, command, priority=1)
            for command in commands
        }

    def probe_commands(self, commands: tuple[str, ...]) -> dict[str, bool]:
        """Probe which commands the receiver responds to.

        Probes use the same retry and cooldown policy as normal queries.
        """
        results: dict[str, bool] = {}
        for command in commands:
            if self._cooldown_remaining():
                raise ConnectionError(
                    f"Receiver cooldown active for {self._host}:{self._port}"
                )
            try:
                self.query_optional(command)
                results[command] = True
            except CommandResponseTimeout:
                results[command] = False
            except (OSError, ConnectionError):
                raise
        return results

    def send(self, command: str, value: str) -> None:
        """Send a raw command without waiting for an acknowledgement."""
        self._with_retry(self._send_once, command, value, retry=False, priority=2)

    def send_volume_write(self, command: str, write: _VolumeWrite) -> None:
        """Claim the latest pending volume target only after winning the socket gate."""
        self._with_retry(
            self._send_claimed_volume_once,
            command,
            write,
            retry=False,
            priority=2,
        )

    def _send_claimed_volume_once(self, command: str, write: _VolumeWrite) -> None:
        self._send_once(command, write.claim())

    def _connect(self) -> eISCP:
        if self._closed:
            raise ConnectionError("Onkyo client is closed")
        if self._device is None:
            self._device = eISCP(self._host, self._port)
        return self._device

    def _ensure_socket_connected(self, device: eISCP) -> None:
        if device.command_socket is not None:
            return
        self.metrics.record_connect_attempt()
        device._ensure_socket_connected()
        if device.command_socket is not None:
            self.metrics.record_connection_success()

    def start_listener(self, callback: Callable[[str], None]) -> None:
        """Start background listener thread for unsolicited ISCP messages."""
        if self._closed or self._listener_thread is not None:
            return
        self._listener_stop.clear()
        self._push_callback = callback
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            name=f"iscp-listener-{self._host}",
            daemon=True,
        )
        self._listener_thread.start()

    def stop_listener(self) -> None:
        """Stop the background listener thread."""
        if self._listener_thread is None:
            return
        self._listener_stop.set()
        self._listener_thread.join(timeout=5.0)
        self._listener_thread = None
        self._push_callback = None

    def _listen_loop(self) -> None:
        """Background thread: listen for unsolicited ISCP messages."""
        _LOGGER.info("Starting ISCP listener for %s:%s", self._host, self._port)
        while not self._listener_stop.is_set():
            remaining = self._cooldown_remaining()
            if remaining:
                self._listener_stop.wait(remaining)
                continue
            message: str | None = None
            failure: Exception | None = None
            with self._operation_lock:
                if self._closed:
                    break
                if self._cooldown_remaining():
                    continue
                try:
                    with self._lock:
                        device = self._connect()
                        self._ensure_socket_connected(device)
                        message = self._read_message(device, 0.2)
                        if message:
                            self.metrics.record_valid_frame()
                except (OSError, TimeoutError, ConnectionError, ValueError) as err:
                    self._record_failure()
                    self._reset_socket()
                    failure = err
                except Exception as err:
                    self._record_failure()
                    self._reset_socket()
                    failure = err
            if failure is not None:
                _LOGGER.debug("Listener connection error, will retry: %s", failure)
                self._wait_cooldown()
                continue
            if message and self._push_callback:
                self._push_callback(message)
        _LOGGER.info("ISCP listener stopped for %s:%s", self._host, self._port)

    def _query_once(self, command: str, timeout: float = _CONNECT_TIMEOUT) -> str:
        with self._lock:
            return self._query_locked(self._connect(), command, timeout)

    def _query_locked(
        self, device: eISCP, command: str, timeout: float = _CONNECT_TIMEOUT
    ) -> str:
        started = time.perf_counter()
        if self._closed:
            raise ConnectionError("Onkyo client is closed")
        self._send_packet(device, f"{command}QSTN")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._ensure_socket_connected(device)
            message = self._read_message(device, min(0.2, deadline - time.monotonic()))
            if message:
                self.metrics.record_valid_frame()
            if message and message[:3] == command:
                self.metrics.record_response(
                    self._query_kind, (time.perf_counter() - started) * 1000
                )
                return message
            if message and self._push_callback:
                self._push_callback(message)
        raise CommandResponseTimeout(f"Timeout waiting for {command} response")

    def _send_packet(self, device: eISCP, command: str) -> None:
        from eiscp.core import command_to_packet

        if device.command_socket is None:
            try:
                self._ensure_socket_connected(device)
            except TimeoutError as err:
                raise ConnectionError("Timed out connecting to receiver") from err
        sock = device.command_socket
        if sock is None:
            raise ConnectionError("Receiver socket was not established")
        packet = memoryview(command_to_packet(command))
        deadline = time.monotonic() + self._CONNECT_TIMEOUT
        while packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [sock], [], remaining)[1]:
                raise TimeoutError("Timed out sending ISCP frame")
            sent = sock.send(packet)
            if sent <= 0:
                raise ConnectionError("Receiver closed the ISCP connection")
            packet = packet[sent:]

    @staticmethod
    def _read_exact(sock: Any, size: int, deadline: float) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([sock], [], [], remaining)[0]:
                if chunks:
                    raise ConnectionError("Incomplete ISCP frame")
                raise TimeoutError("Timeout waiting for ISCP frame")
            chunk = sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Receiver closed the ISCP connection")
            chunks.extend(chunk)
        return bytes(chunks)

    @classmethod
    def _read_message(cls, device: eISCP, timeout: float) -> str | None:
        sock = device.command_socket
        if sock is None:
            try:
                device._ensure_socket_connected()
            except TimeoutError as err:
                raise ConnectionError("Timed out connecting to receiver") from err
            sock = device.command_socket
        if sock is None or not select.select([sock], [], [], timeout)[0]:
            return None
        deadline = time.monotonic() + timeout
        header = cls._read_exact(sock, 16, deadline)
        magic, header_size, data_size, _version, _reserved = struct.unpack(
            "!4sIIB3s", header
        )
        if magic != b"ISCP" or header_size != 16 or data_size > 65536:
            raise ValueError("Invalid ISCP frame header")
        try:
            body = cls._read_exact(sock, data_size, deadline)
        except TimeoutError as err:
            raise ConnectionError("Incomplete ISCP frame body") from err
        from eiscp.core import ISCPMessage

        return ISCPMessage.parse(body.decode("utf-8"))

    def _send_once(self, command: str, value: str) -> None:
        with self._lock:
            self._send_packet(self._connect(), f"{command}{value}")

    def _with_retry(
        self, method: Any, *args: Any, retry: bool = True, priority: int = 0
    ) -> Any:
        for attempt in range(self._retries if retry else 1):
            self._operation_lock.acquire(priority)
            if self._closed or self._cooldown_remaining():
                closed = self._closed
                self._operation_lock.release()
                if closed:
                    raise ConnectionError("Onkyo client is closed")
                raise ConnectionError(
                    f"Receiver cooldown active for {self._host}:{self._port}"
                )
            try:
                result = method(*args)
                self._consecutive_failures = 0
                self._circuit_open_until = 0.0
                return result
            except self._RETRYABLE_ERRORS + (ValueError,) as err:
                self._record_failure()
                _LOGGER.debug(
                    "ISCP command failed on attempt %s/%s: %s",
                    attempt + 1,
                    self._retries,
                    err,
                )
                self._reset_socket()
                if attempt >= (self._retries if retry else 1) - 1:
                    raise
            finally:
                self._operation_lock.release()
            self._wait_cooldown()
        raise AssertionError("retry loop exhausted")

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        delay = min(
            self._COOLDOWN_BASE
            * (2 ** min(self._consecutive_failures - 1, self._COOLDOWN_MAX_EXPONENT)),
            self._COOLDOWN_MAX,
        )
        self._circuit_open_until = max(
            self._circuit_open_until, time.monotonic() + delay
        )

    def _cooldown_remaining(self) -> float:
        return max(0.0, self._circuit_open_until - time.monotonic())

    def cooldown_remaining(self) -> float:
        """Return seconds remaining in the shared transport cooldown."""
        return self._cooldown_remaining()

    def _wait_cooldown(self) -> None:
        self._listener_stop.wait(self._cooldown_remaining())


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
    initial_refresh_task: Any = None
    stop_listener_unsub: Any = None
    discovery_task: Any = None
    discovery_states: dict[str, str] = field(default_factory=dict)
    discovery_timeouts: dict[str, int] = field(default_factory=dict)
    capability_timeouts: dict[str, int] = field(default_factory=dict)
    capability_cursor: int = 0
    platform_adders: dict[str, Callable[[], None]] = field(default_factory=dict)
    platform_unique_ids: dict[str, set[str]] = field(default_factory=dict)
    stopping: bool = False


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
            update_interval=None,
            always_update=False,
        )
        self.scan_interval = max(0, update_interval)
        self.client = client
        self.host = host
        self.device_name = name
        self.zone = zone
        self.source_raw_to_name = source_raw_to_name
        self.source_name_to_raw = source_name_to_raw
        self.command_capabilities: dict[str, bool] = {}
        self.data = OnkyoState()
        self._optional_cursor = 0
        self._optional_failures: dict[str, tuple[int, float]] = {}
        self._priority_optional: list[str] = []
        self._optional_reconcile_at: dict[str, float] = {}
        self._optional_last_push_at: dict[str, float] = {}
        self._active_optional_commands: set[str] = set()
        self._optional_reconcile_interval = max(60.0, self.scan_interval * 6)
        self._push_revisions = dict.fromkeys(("power", "volume", "muted", "source"), 0)
        self._volume_observation_revision = 0
        self._valid_volume_push_revision = 0
        self._optional_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._optional_push_revisions: dict[str, int] = {}
        self._listening_revision = 0
        self._closing = False
        self._core_poll_succeeded = False
        self._core_poll_completed_at = 0.0
        self._core_poll_latency_ms = _LatencySummary()
        self._optional_poll_latency_ms = _LatencySummary()
        self._write_queue: asyncio.Queue[_WriteIntent] = asyncio.Queue()
        self._write_task: asyncio.Task[None] | None = None
        self._pending_absolute_volume: _WriteIntent | None = None
        self._volume_confirmation_tasks: set[asyncio.Task[None]] = set()
        self._volume_confirmation_intents: list[_WriteIntent] = []
        self._write_completion_tasks: set[asyncio.Task[None]] = set()
        self._write_completion_intents: list[_WriteIntent] = []
        self._volume_write_sequence = 0
        self._latest_volume_write_sequence = 0
        self._volume_confirmation_grace = 0.1

    def set_command_capability(self, command: str, supported: bool) -> None:
        self.command_capabilities[command] = supported

    def supports(self, command: str) -> bool:
        return self.command_capabilities.get(command, False)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return bounded client and coordinator metrics without raw timestamps."""
        remaining = self.client.cooldown_remaining()
        return {
            **self.client.metrics.snapshot(),
            "core_poll_duration_ms": self._core_poll_latency_ms.snapshot(),
            "optional_poll_duration_ms": self._optional_poll_latency_ms.snapshot(),
            "cooldown_remaining_seconds": round(remaining, 3),
        }

    @property
    def core_power_is_fresh(self) -> bool:
        """Whether the last complete core poll recently confirmed power state."""
        if not self._core_poll_succeeded:
            return False
        max_age = max(30.0, self.scan_interval * 2) if self.scan_interval else 30.0
        return time.monotonic() - self._core_poll_completed_at <= max_age

    def async_start_periodic_refresh(self) -> None:
        """Start fixed-deadline core polling, unaffected by push notifications."""
        if self.scan_interval <= 0 or self._closing:
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self.hass.async_create_background_task(
                self._async_periodic_refresh(),
                name=f"Onkyo core refresh {self.host} {self.zone.key}",
            )

    async def _async_periodic_refresh(self) -> None:
        """Refresh at fixed deadlines, skipping missed ticks instead of catching up."""
        interval = float(self.scan_interval)
        deadline = asyncio.get_running_loop().time() + interval
        while not self._closing:
            await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
            if self._closing:
                return
            try:
                await self.async_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.debug("Scheduled core refresh failed for %s", self.name)
            deadline += interval
            now = asyncio.get_running_loop().time()
            if deadline <= now:
                deadline = now + interval

    async def async_probe_command(self, command: str) -> bool:
        return await self._async_probe_optional(command)

    async def _async_update_data(self) -> OnkyoState:
        started = time.perf_counter()
        try:
            return await self._async_fetch_core_state()
        finally:
            self._core_poll_latency_ms.record((time.perf_counter() - started) * 1000)

    async def _async_fetch_core_state(self) -> OnkyoState:
        """Fetch the latest state from the device using batched queries."""
        self._core_poll_succeeded = False
        # Query-start boundaries conservatively prefer pushes during that query.
        # Each field uses its own batch boundary, so later polls replace older pushes.
        poll_revisions = {
            "power": self._push_revisions["power"],
            "volume_observation": self._volume_observation_revision,
        }
        try:
            power_results: dict[str, str] = await self.hass.async_add_executor_job(
                self.client.query_batch, (self.zone.power_command,)
            )
        except Exception as err:
            raise UpdateFailed(f"Power refresh failed: {err}") from err
        power_raw = power_results.get(self.zone.power_command)
        if power_raw is None:
            raise UpdateFailed(
                f"Core command {self.zone.power_command} missing from response"
            )
        powered_on = _parse_power(power_raw, self.zone.power_command)
        previous = self.data or OnkyoState()
        if self._closing:
            raise asyncio.CancelledError
        if not powered_on:
            self._core_poll_succeeded = True
            self._core_poll_completed_at = time.monotonic()
            return self._merge_newer_pushes(
                replace(previous, power=False), poll_revisions
            )

        core_commands = (
            self.zone.volume_command,
            self.zone.mute_command,
            self.zone.source_command,
        )
        poll_revisions.update(
            (name, self._push_revisions[name]) for name in ("volume", "muted", "source")
        )
        poll_revisions["volume_observation"] = self._volume_observation_revision
        try:
            results: dict[str, str] = await self.hass.async_add_executor_job(
                self.client.query_batch, core_commands
            )
        except Exception as err:
            raise UpdateFailed(f"State refresh failed: {err}") from err
        if self._closing:
            raise asyncio.CancelledError

        for cmd in core_commands:
            if cmd not in results:
                raise UpdateFailed(f"Core command {cmd} missing from batch response")

        state = OnkyoState(
            power=True,
            volume=_parse_volume(
                results[self.zone.volume_command], self.zone.volume_command
            ),
            muted=_parse_mute(results[self.zone.mute_command], self.zone.mute_command),
            source=_parse_enum(
                results[self.zone.source_command], self.source_raw_to_name
            ),
        )
        # Optional queries can finish while this core batch is in flight. Use
        # the latest snapshot so the core result retains those newer fields.
        latest = self.data or previous
        state = _apply_previous_fallbacks(state, latest)
        self._start_optional_refresh()
        self._core_poll_succeeded = True
        self._core_poll_completed_at = time.monotonic()
        return self._merge_newer_pushes(state, poll_revisions)

    def _start_optional_refresh(self) -> None:
        """Keep at most one optional polling task queued or running."""
        if self._closing or not self._optional_command_set():
            return
        if self._optional_task is None or self._optional_task.done():
            self._optional_task = self.hass.async_create_background_task(
                self._async_optional_refresh(),
                name=f"Onkyo optional refresh {self.host} {self.zone.key}",
            )

    async def async_cancel_optional_refresh(self) -> None:
        """Stop optional and periodic polling during integration cleanup."""
        self._closing = True
        tasks = (self._optional_task, self._poll_task)
        active = tuple(task for task in tasks if task is not None and not task.done())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def async_cancel_writes(self) -> None:
        """Cancel queued writes and settle all callers during runtime cleanup."""
        task = self._write_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        confirmation_tasks = tuple(self._volume_confirmation_tasks)
        for confirmation in confirmation_tasks:
            confirmation.cancel()
        if confirmation_tasks:
            await asyncio.gather(*confirmation_tasks, return_exceptions=True)
        completion_tasks = tuple(self._write_completion_tasks)
        for completion in completion_tasks:
            completion.cancel()
        if completion_tasks:
            await asyncio.gather(*completion_tasks, return_exceptions=True)
        for intent in self._volume_confirmation_intents:
            self._settle_write(intent, HomeAssistantError("Onkyo coordinator stopped"))
        self._volume_confirmation_intents.clear()
        for intent in self._write_completion_intents:
            self._settle_write(intent, HomeAssistantError("Onkyo coordinator stopped"))
        self._write_completion_intents.clear()
        self._pending_absolute_volume = None
        while not self._write_queue.empty():
            intent = self._write_queue.get_nowait()
            self._settle_write(intent, HomeAssistantError("Onkyo coordinator stopped"))

    async def _async_optional_refresh(self) -> None:
        started = time.perf_counter()
        try:
            await self._async_run_optional_refresh()
        finally:
            self._optional_poll_latency_ms.record(
                (time.perf_counter() - started) * 1000
            )

    async def _async_run_optional_refresh(self) -> None:
        """Fetch a small optional batch after core state has been published."""
        commands = tuple(sorted(self._optional_command_set()))
        if not commands:
            return
        now = time.monotonic()
        due_commands = tuple(
            command
            for command in commands
            if not (
                command in self._optional_reconcile_at
                and now < self._optional_reconcile_at[command]
                and now - self._optional_last_push_at.get(command, 0.0)
                < self._optional_reconcile_interval
            )
        )
        if not due_commands and not self._priority_optional:
            return
        count = min(2, len(due_commands))
        start = self._optional_cursor % len(commands)
        rotated_commands = tuple(
            commands[(start + offset) % len(commands)] for offset in range(len(commands))
        )
        rotated = tuple(command for command in rotated_commands if command in due_commands)[
            :count
        ]
        self._optional_cursor = (start + max(1, count)) % len(commands)
        prioritized = tuple(
            command for command in self._priority_optional if command in commands
        )
        selected = tuple(dict.fromkeys(prioritized + rotated))[:2]
        for command in selected:
            if command not in self._optional_command_set():
                continue
            failures, retry_at = self._optional_failures.get(command, (0, 0.0))
            if time.monotonic() < retry_at:
                continue
            push_revision = self._optional_push_revisions.get(command, 0)
            listening_revision = self._listening_revision
            try:
                raw = await self.hass.async_add_executor_job(
                    self.client.query_optional, command
                )
                self._optional_failures.pop(command, None)
                if command in self._priority_optional:
                    self._priority_optional.remove(command)
                if (
                    self._optional_push_revisions.get(command, 0) == push_revision
                    and (
                        command not in {"LMD", "IFA"}
                        or self._listening_revision == listening_revision
                    )
                ):
                    self._apply_optional_response(command, raw)
                    self._optional_reconcile_at.pop(command, None)
                    self._optional_last_push_at.pop(command, None)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Optional command %s failed: %s", command, err)
                failures += 1
                self._optional_failures[command] = (
                    failures,
                    time.monotonic() + min(30 * (2 ** min(failures - 1, 6)), 1800),
                )
                if command in self._priority_optional:
                    self._priority_optional.remove(command)

    def _optional_response_state(self, command: str, raw: str) -> OnkyoState:
        """Build state after applying one optional response."""
        state = replace(
            self.data,
            audio_information=dict(self.data.audio_information),
            video_information=dict(self.data.video_information),
        )
        if command == "LMD":
            # A fresh LMD observation supersedes the display cached by older IFA.
            state.audio_information.pop("listening_mode", None)
        if command in dict(_OPTIONAL_MODE_COMMANDS):
            value_map = dict(_OPTIONAL_MODE_COMMANDS)[command] or LISTENING_RAW_TO_NAME
            setattr(state, _OPTIONAL_ATTR_MAP[command], _parse_enum(raw, value_map))
        elif command in dict(_OPTIONAL_SWITCH_COMMANDS):
            setattr(
                state,
                _OPTIONAL_ATTR_MAP[command],
                _parse_switch(raw, dict(_OPTIONAL_SWITCH_COMMANDS)[command]),
            )
        elif command == "SLP":
            state.sleep_minutes = _parse_sleep(raw)
        elif command in _OPTIONAL_LEVEL_COMMANDS:
            setattr(state, _OPTIONAL_ATTR_MAP[command], _parse_signed_level(raw))
        elif command == "TUN":
            state.tuner_frequency = _parse_tuner_frequency(raw)
        elif command in _OPTIONAL_INFORMATION_COMMANDS:
            parsed = _INFORMATION_PARSERS[command](raw)
            attr = _OPTIONAL_ATTR_MAP[command]
            if command in _SINGLE_VALUE_INFORMATION or attr == "video_information":
                getattr(state, attr).update(parsed)
            else:
                setattr(state, attr, parsed)
        state.listening_mode = _resolve_listening_mode_display(
            state.listening_mode, state.audio_information
        )
        return state

    def _apply_optional_response(self, command: str, raw: str) -> None:
        """Merge one optional response without notifying unchanged observations."""
        if self._closing:
            return
        state = self._optional_response_state(command, raw)
        if command in {"LMD", "IFA"}:
            self._listening_revision += 1
        if state != self.data or not self.last_update_success:
            self.async_set_updated_data(state)

    def _merge_newer_pushes(
        self, state: OnkyoState, poll_revisions: dict[str, int]
    ) -> OnkyoState:
        """Preserve pushes applied since each field's poll query began."""
        for name, revision in poll_revisions.items():
            if name == "volume_observation":
                if self._volume_observation_revision != revision:
                    state.volume = self.data.volume
                continue
            if self._push_revisions[name] != revision:
                setattr(state, name, getattr(self.data, name))
        return replace(
            state,
            audio_information=dict(state.audio_information),
            video_information=dict(state.video_information),
        )

    def _optional_command_set(self) -> set[str]:
        """Return supported optional commands demanded by active entities."""
        contexts = set()
        async_contexts = getattr(self, "async_contexts", None)
        if async_contexts is not None:
            for context in async_contexts():
                if isinstance(context, str):
                    contexts.add(context)
                elif isinstance(context, (set, frozenset, tuple, list)):
                    contexts.update(item for item in context if isinstance(item, str))
        commands: set[str] = set()
        if self.zone.key != "main":
            for command in self.zone.optional_commands:
                if command in contexts and self.supports(command):
                    commands.add(command)
            self._reset_reconcile_deadlines_for_new_demand(commands)
            commands.update(
                command
                for command in self._priority_optional
                if self.supports(command) and command in self.zone.optional_commands
            )
            return commands
        for command, _ in _OPTIONAL_MODE_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        for command, _ in _OPTIONAL_SWITCH_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        for command in _OPTIONAL_SLEEP_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        for command in _OPTIONAL_LEVEL_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        for command in _OPTIONAL_TUNER_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        for command in _OPTIONAL_INFORMATION_COMMANDS:
            if command in contexts and self.supports(command):
                commands.add(command)
        self._reset_reconcile_deadlines_for_new_demand(commands)
        commands.update(
            command for command in self._priority_optional if self.supports(command)
        )
        return commands

    def _reset_reconcile_deadlines_for_new_demand(self, commands: set[str]) -> None:
        """Make a command query immediately when its first listener appears."""
        for command in commands - self._active_optional_commands:
            self._optional_reconcile_at.pop(command, None)
            self._optional_last_push_at.pop(command, None)
        self._active_optional_commands = set(commands)

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
            _LOGGER.info(
                "%s does not respond to %s queries: %s", self.host, command, err
            )
            return False
        return True

    async def async_turn_on(self) -> None:
        await self._async_send(self.zone.power_command, "01")

    async def async_turn_off(self) -> None:
        await self._async_send(self.zone.power_command, "00")

    async def async_set_muted(self, muted: bool) -> None:
        await self._async_send(self.zone.mute_command, "01" if muted else "00")

    async def async_volume_step(self, up: bool) -> None:
        await self._async_send(self.zone.volume_command, "UP1" if up else "DOWN1")

    async def async_set_volume(
        self, volume_level: float, max_volume: int = DEFAULT_MAX_VOLUME
    ) -> None:
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
        raw = DIMMER_NAME_TO_RAW.get(dimmer_level.lower())
        if raw is None and dimmer_level.upper() in DIMMER_RAW_TO_NAME:
            raw = dimmer_level.upper()
        if raw is None:
            raise HomeAssistantError(f"Invalid dimmer level: {dimmer_level}")
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

    def _handle_push_message(self, message: str) -> None:
        """Handle an unsolicited ISCP message from the listener thread."""
        self.hass.loop.call_soon_threadsafe(self._apply_push_message, message)

    def _apply_push_message(self, message: str) -> None:
        """Apply a listener update on the Home Assistant event loop."""
        if self._closing or len(message) < 3:
            return
        prefix = message[:3]
        state: OnkyoState | None = None
        if prefix == self.zone.power_command:
            state = OnkyoState(
                power=_parse_power(message, prefix),
                volume=self.data.volume,
                muted=self.data.muted,
                source=self.data.source,
                **self._optional_state_dict(),
            )
        elif prefix == self.zone.volume_command:
            parsed_volume = _parse_volume(message, prefix)
            state = OnkyoState(
                power=self.data.power,
                volume=parsed_volume,
                muted=self.data.muted,
                source=self.data.source,
                **self._optional_state_dict(),
            )
            self._volume_observation_revision += 1
            if parsed_volume is not None:
                self._valid_volume_push_revision += 1
            for intent in tuple(self._volume_confirmation_intents):
                if intent.push_event is not None:
                    intent.push_event.set()
        elif prefix == self.zone.mute_command:
            state = OnkyoState(
                power=self.data.power,
                volume=self.data.volume,
                muted=_parse_mute(message, prefix),
                source=self.data.source,
                **self._optional_state_dict(),
            )
        elif prefix == self.zone.source_command:
            state = OnkyoState(
                power=self.data.power,
                volume=self.data.volume,
                muted=self.data.muted,
                source=_parse_enum(message, self.source_raw_to_name),
                **self._optional_state_dict(),
            )
        elif self.zone.key == "main" and prefix in _OPTIONAL_ATTR_MAP:
            try:
                state = self._optional_response_state(prefix, message)
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Ignoring malformed optional push %s", message)
                return
            self._optional_push_revisions[prefix] = (
                self._optional_push_revisions.get(prefix, 0) + 1
            )
            pushed_at = time.monotonic()
            self._optional_last_push_at[prefix] = pushed_at
            self._optional_reconcile_at.setdefault(
                prefix, pushed_at + self._optional_reconcile_interval
            )
            if prefix in {"LMD", "IFA"}:
                self._listening_revision += 1

        if state is not None:
            field_name = {
                self.zone.power_command: "power",
                self.zone.volume_command: "volume",
                self.zone.mute_command: "muted",
                self.zone.source_command: "source",
            }.get(prefix)
            if field_name is not None:
                self._push_revisions[field_name] += 1
            if state != self.data or not self.last_update_success:
                _LOGGER.debug("Push update: %s -> %s", prefix, message[3:])
                self.async_set_updated_data(state)

    def _optional_state_dict(self) -> dict[str, Any]:
        """Copy optional fields from current state for partial updates."""
        return {
            "listening_mode": self.data.listening_mode,
            "dimmer_level": self.data.dimmer_level,
            "audio_selector": self.data.audio_selector,
            "late_night_mode": self.data.late_night_mode,
            "audyssey_dynamic_volume": self.data.audyssey_dynamic_volume,
            "cinema_filter": self.data.cinema_filter,
            "audyssey_dynamic_eq": self.data.audyssey_dynamic_eq,
            "music_optimizer": self.data.music_optimizer,
            "trigger_a": self.data.trigger_a,
            "trigger_b": self.data.trigger_b,
            "trigger_c": self.data.trigger_c,
            "sleep_minutes": self.data.sleep_minutes,
            "center_level": self.data.center_level,
            "subwoofer_level": self.data.subwoofer_level,
            "tuner_frequency": self.data.tuner_frequency,
            "audio_information": dict(self.data.audio_information),
            "video_information": dict(self.data.video_information),
        }

    async def async_set_boolean_option(self, command: str, enabled: bool) -> None:
        await self._async_send(command, "01" if enabled else "00")

    async def _async_send(self, command: str, value: str) -> None:
        if self._closing:
            raise HomeAssistantError("Onkyo coordinator stopped")
        future: asyncio.Future[None] = self.hass.loop.create_future()
        future.add_done_callback(
            lambda completed: completed.exception()
            if not completed.cancelled()
            else None
        )
        is_volume = command == self.zone.volume_command
        absolute = is_volume and len(value) == 2 and all(
            char in "0123456789ABCDEFabcdef" for char in value
        )
        if absolute and self._pending_absolute_volume is not None:
            pending = self._pending_absolute_volume
            assert pending.volume_write is not None
            if pending.volume_write.update(value):
                pending.absolute_target = int(value, 16)
                pending.waiters.append(future)
                await asyncio.shield(future)
                return

        if is_volume:
            self._volume_write_sequence += 1
            write = _VolumeWrite(value, lambda: self._valid_volume_push_revision)
            intent = _WriteIntent(
                command,
                value,
                future,
                volume_write=write,
                absolute_target=int(value, 16) if absolute else None,
                sequence=self._volume_write_sequence,
                push_event=asyncio.Event(),
            )
            self._latest_volume_write_sequence = intent.sequence
            self._pending_absolute_volume = intent if absolute else None
        else:
            intent = _WriteIntent(command, value, future)
            self._pending_absolute_volume = None
        self._write_queue.put_nowait(intent)
        if self._write_task is None or self._write_task.done():
            self._write_task = self.hass.async_create_background_task(
                self._async_write_pump(),
                name=f"Onkyo write queue {self.host} {self.zone.key}",
            )
        await asyncio.shield(future)

    async def _async_write_pump(self) -> None:
        """Send queued commands in order, coalescing only unclaimed absolutes."""
        while not self._closing and not self._write_queue.empty():
            intent = await self._write_queue.get()
            try:
                if intent.volume_write is not None:
                    sender = getattr(self.client, "send_volume_write", None)
                    if sender is None:
                        await self.hass.async_add_executor_job(
                            self.client.send,
                            intent.command,
                            intent.volume_write.claim(),
                        )
                    else:
                        await self.hass.async_add_executor_job(
                            sender, intent.command, intent.volume_write
                        )
                    self._volume_confirmation_intents.append(intent)
                    confirmation = self.hass.async_create_background_task(
                        self._finish_volume_write(intent),
                        name=f"Onkyo volume confirmation {self.host} {self.zone.key}",
                    )
                    self._volume_confirmation_tasks.add(confirmation)
                    confirmation.add_done_callback(
                        self._volume_confirmation_tasks.discard
                    )
                    continue
                else:
                    await self.hass.async_add_executor_job(
                        self.client.send, intent.command, intent.value
                    )
                    if (
                        intent.command in _OPTIONAL_ATTR_MAP
                        and self.supports(intent.command)
                        and (
                            self.zone.key == "main"
                            or intent.command in self.zone.optional_commands
                        )
                    ):
                        if intent.command in self._priority_optional:
                            self._priority_optional.remove(intent.command)
                        self._priority_optional.insert(0, intent.command)
                    self._optional_failures.pop(intent.command, None)
                    self._write_completion_intents.append(intent)
                    completion = self.hass.async_create_background_task(
                        self._finish_nonvolume_write(intent),
                        name=f"Onkyo write refresh {self.host} {self.zone.key}",
                    )
                    self._write_completion_tasks.add(completion)
                    completion.add_done_callback(self._write_completion_tasks.discard)
                    continue
                self._settle_write(intent, None)
            except asyncio.CancelledError:
                self._settle_write(
                    intent, HomeAssistantError("Onkyo coordinator stopped")
                )
                raise
            except (ConnectionError, OSError) as err:
                self._settle_write(
                    intent,
                    HomeAssistantError(
                        "Onkyo receiver is temporarily unavailable (circuit breaker open)"
                    ),
                )
            except Exception as err:
                self._settle_write(intent, err)
            finally:
                if self._pending_absolute_volume is intent:
                    self._pending_absolute_volume = None

    async def _confirm_volume_write(self, intent: _WriteIntent) -> None:
        """Confirm an absolute volume target from a push or one targeted query."""
        write = intent.volume_write
        assert write is not None
        target = intent.absolute_target
        if target is None:
            return
        if intent.sequence < self._latest_volume_write_sequence:
            return
        if not self._volume_push_matches(write.start_revision, target):
            await self._wait_for_volume_push(intent, write.start_revision, target)
        if self._volume_push_matches(write.start_revision, target):
            return
        if intent.sequence < self._latest_volume_write_sequence:
            return
        await self._query_volume_confirmation(intent, target)

    async def _confirm_relative_volume_write(self, intent: _WriteIntent) -> None:
        """Confirm relative steps with a fresh observation or a targeted query."""
        write = intent.volume_write
        assert write is not None
        if intent.sequence < self._latest_volume_write_sequence:
            return
        if self._valid_volume_push_revision > write.start_revision:
            return
        await self._wait_for_volume_push(intent, write.start_revision, None)
        if self._valid_volume_push_revision > write.start_revision:
            return
        await self._query_volume_confirmation(intent, None)

    async def _wait_for_volume_push(
        self, intent: _WriteIntent, revision: int, target: int | None
    ) -> None:
        event = intent.push_event
        if event is None:
            return
        if (
            self._valid_volume_push_revision == revision
            and (target is None or self.data.volume != target)
        ):
            try:
                await asyncio.wait_for(event.wait(), self._volume_confirmation_grace)
            except TimeoutError:
                pass

    async def _finish_volume_write(self, intent: _WriteIntent) -> None:
        try:
            if intent.absolute_target is not None:
                await self._confirm_volume_write(intent)
            else:
                await self._confirm_relative_volume_write(intent)
            self._settle_write(intent, None)
        except asyncio.CancelledError:
            self._settle_write(intent, HomeAssistantError("Onkyo coordinator stopped"))
            raise
        except Exception as err:
            self._settle_write(intent, err)
        finally:
            if intent in self._volume_confirmation_intents:
                self._volume_confirmation_intents.remove(intent)

    async def _finish_nonvolume_write(self, intent: _WriteIntent) -> None:
        try:
            await self.async_request_refresh()
            self._settle_write(intent, None)
        except asyncio.CancelledError:
            self._settle_write(intent, HomeAssistantError("Onkyo coordinator stopped"))
            raise
        except Exception as err:
            self._settle_write(intent, err)
        finally:
            if intent in self._write_completion_intents:
                self._write_completion_intents.remove(intent)

    def _volume_push_matches(self, revision: int, target: int) -> bool:
        return (
            self._valid_volume_push_revision > revision
            and self.data.volume == target
        )

    async def _query_volume_confirmation(
        self, intent: _WriteIntent, target: int | None
    ) -> None:
        if self._closing:
            raise HomeAssistantError("Onkyo coordinator stopped")
        if intent.sequence < self._latest_volume_write_sequence:
            return
        revision = self._push_revisions["volume"]
        try:
            raw = await self.hass.async_add_executor_job(
                self.client.query_optional, intent.command, 1.5
            )
        except Exception as err:
            raise HomeAssistantError("Unable to confirm receiver volume") from err
        if self._closing:
            raise HomeAssistantError("Onkyo coordinator stopped")
        observed = _parse_volume(raw, intent.command)
        if observed is None:
            raise HomeAssistantError("Receiver returned no numeric volume")
        fresh_query_observation = (
            intent.sequence == self._latest_volume_write_sequence
            and self._push_revisions["volume"] == revision
        )
        if fresh_query_observation:
            self._volume_observation_revision += 1
            state = replace(self.data, volume=observed)
            if state != self.data:
                self.async_set_updated_data(state)
        if (
            intent.sequence == self._latest_volume_write_sequence
            and target is not None
            and not (
            (fresh_query_observation and observed == target)
            or self._volume_push_matches(
                intent.volume_write.start_revision, target
            )
            )
        ):
            raise HomeAssistantError(
                f"Receiver did not confirm requested volume {target}"
            )

    @staticmethod
    def _settle_write(intent: _WriteIntent, error: BaseException | None) -> None:
        for future in intent.waiters:
            if future.done():
                continue
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(error)


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
    retries: int = 2,
    strict_sources: bool = True,
) -> OnkyoRuntimeData:
    """Build runtime data for one configured device."""
    normalized_model = _normalize_model(model)
    profile = PROFILES.get(normalized_model, GENERIC_PROFILE)
    normalized_sources, skipped = _normalize_sources(
        sources or profile.default_sources,
        strict=strict_sources,
    )
    if skipped:
        _LOGGER.warning(
            "Skipped %d unknown source(s) for %s: %s",
            len(skipped),
            host,
            ", ".join(sorted(skipped)),
        )
    client = OnkyoLegacyClient(host, port, retries=retries)
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
    source_lookup = _build_source_lookup(
        normalized_sources, main_raw_to_name, main_name_to_raw
    )
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
        queryable_commands=profile.queryable_commands,
        source_lookup=source_lookup,
        supported_listening_modes=_build_select_options("LMD"),
        supported_audio_selectors=_build_select_options("SLA")
        if "SLA" in profile.queryable_commands
        else [],
        supported_late_night_modes=_build_select_options("LTN")
        if "LTN" in profile.queryable_commands
        else [],
        supported_audyssey_volume_modes=_build_select_options("ADV")
        if "ADV" in profile.queryable_commands
        else [],
        supported_dimmer_modes=_build_select_options("DIM"),
        entity_ids=entity_ids,
    )

    candidate_zone_keys = profile.supported_zones
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


def register_dynamic_entities(
    runtime: OnkyoRuntimeData,
    platform: str,
    add_entities: Callable[[list[Any]], None],
    build_entities: Callable[[], list[Any]],
) -> None:
    """Register an idempotent platform callback for newly discovered support."""
    seen = runtime.platform_unique_ids.setdefault(platform, set())
    try:
        from homeassistant.helpers.entity_platform import (
            async_get_current_platform,
            async_get_platforms,
        )

        platform_instance = async_get_current_platform()
    except (ImportError, RuntimeError):
        # Lightweight tests don't model HA's current-platform context.
        platform_instance = None
        async_get_platforms = None

    def add_new_entities() -> None:
        if runtime.stopping:
            return
        if platform_instance is not None and async_get_platforms is not None:
            if platform_instance not in async_get_platforms(
                runtime.coordinator.hass, DOMAIN
            ):
                if runtime.platform_adders.get(platform) is add_new_entities:
                    runtime.platform_adders.pop(platform, None)
                return
        entities = []
        new_ids = []
        for entity in build_entities():
            unique_id = getattr(entity, "unique_id", None) or getattr(
                entity, "_attr_unique_id", None
            )
            if unique_id and unique_id not in seen:
                entities.append(entity)
                new_ids.append(unique_id)
        if entities:
            add_entities(entities)
            seen.update(new_ids)

    runtime.platform_adders[platform] = add_new_entities
    add_new_entities()


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


def _normalize_sources(
    sources: dict[str, str],
    *,
    strict: bool = True,
) -> tuple[dict[str, str], set[str]]:
    source_raw_to_name, source_name_to_raw = SOURCE_MAPS["main"]
    normalized: dict[str, str] = {}
    skipped: set[str] = set()
    for label, alias in sources.items():
        raw = alias.upper()
        if raw in source_raw_to_name:
            canonical_alias = source_raw_to_name[raw]
            normalized[_display_source_name(canonical_alias)] = canonical_alias
            continue

        normalized_alias = alias.lower()
        if normalized_alias not in source_name_to_raw:
            if strict:
                raise UpdateFailed(
                    f"Unsupported source alias in configuration: {alias}"
                )
            _LOGGER.warning("Skipping unknown source alias %r in configuration", alias)
            skipped.add(alias)
            continue
        canonical_alias = source_raw_to_name[source_name_to_raw[normalized_alias]]
        normalized[_display_source_name(canonical_alias)] = canonical_alias
    return normalized, skipped


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
    return candidate


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


def _parse_resolution(response: str) -> dict[str, str]:
    payload = _payload_code(response, "RES")
    return {"video_resolution": payload}


def _parse_hdmi_output(response: str) -> dict[str, str]:
    payload = _payload_code(response, "HDO")
    return {"hdmi_output": payload}


_INFORMATION_PARSERS: dict[str, Any] = {
    "IFA": _parse_audio_information,
    "IFV": _parse_video_information,
    "RES": _parse_resolution,
    "HDO": _parse_hdmi_output,
}


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
