"""Diagnostics support for Onkyo Legacy."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"host", "port"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime.coordinator
    client = coordinator.client
    metrics_snapshot = getattr(coordinator, "metrics_snapshot", None)
    metrics = metrics_snapshot() if metrics_snapshot is not None else {}
    zone_poll_durations = {}
    for zone_runtime in (runtime, *runtime.zones):
        zone_coordinator = getattr(zone_runtime, "coordinator", None)
        if zone_coordinator is None:
            continue
        core_summary = getattr(zone_coordinator, "_core_poll_latency_ms", None)
        optional_summary = getattr(zone_coordinator, "_optional_poll_latency_ms", None)
        if core_summary is None or optional_summary is None:
            if zone_runtime is runtime:
                zone_poll_durations[getattr(zone_runtime, "zone_key", "main")] = {
                    "core_poll_duration_ms": metrics.get("core_poll_duration_ms"),
                    "optional_poll_duration_ms": metrics.get("optional_poll_duration_ms"),
                }
            continue
        zone_poll_durations[zone_runtime.zone_key] = {
            "core_poll_duration_ms": core_summary.snapshot(),
            "optional_poll_duration_ms": optional_summary.snapshot(),
        }
    cooldown_remaining = getattr(client, "cooldown_remaining", lambda: 0.0)()
    consecutive_failures = getattr(client, "_consecutive_failures", 0)

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "model": runtime.model,
        "zones": [zone.zone_key for zone in runtime.zones],
        "queryable_commands": list(runtime.queryable_commands),
        "coordinator_data": asdict(coordinator.data) if coordinator.data else None,
        "circuit_breaker": {
            "consecutive_failures": consecutive_failures,
            "cooldown_remaining_seconds": round(cooldown_remaining, 3),
        },
        "metrics": metrics,
        "zone_poll_durations": zone_poll_durations,
    }
