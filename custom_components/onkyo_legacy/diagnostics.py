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

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "model": runtime.model,
        "zones": [zone.zone_key for zone in runtime.zones],
        "queryable_commands": list(runtime.queryable_commands),
        "coordinator_data": asdict(coordinator.data) if coordinator.data else None,
        "circuit_breaker": {
            "consecutive_failures": coordinator.client._consecutive_failures,
            "circuit_open_until": coordinator.client._circuit_open_until,
        },
    }
