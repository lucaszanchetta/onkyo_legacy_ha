"""Config-entry based integration for legacy Onkyo processors."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_LISTENING_MODE,
    CONF_MODEL,
    CONF_MAX_VOLUME,
    CONF_SCAN_INTERVAL,
    CONF_SOURCES,
    DEFAULT_MODEL,
    DEFAULT_MAX_VOLUME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    PROFILE_DEFAULT_NAMES,
    PROFILE_DEFAULT_SOURCES,
    PROFILE_QUERYABLE_COMMANDS,
    SERVICE_REFRESH,
    SERVICE_SET_LISTENING_MODE,
)
from .coordinator import OnkyoRuntimeData, build_runtime_data

_LOGGER = logging.getLogger(__name__)

MAIN_ENTITY_UNIQUE_ID_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("number", "main-volume-level", "volume-level"),
    ("number", "main-sleep-timer", "sleep-timer"),
    ("number", "main-center-level", "center-level"),
    ("number", "main-subwoofer-level", "subwoofer-level"),
    ("select", "main-source", "source"),
    ("select", "main-listening-mode", "listening-mode"),
    ("select", "main-dimmer-level", "dimmer-level"),
    ("select", "main-audio-selector", "audio-selector"),
    ("select", "main-late-night", "late-night"),
    ("select", "main-audyssey-dynamic-volume", "audyssey-dynamic-volume"),
    ("switch", "main-power", "power"),
    ("switch", "main-mute", "mute"),
    ("switch", "main-cinema_filter", "cinema_filter"),
    ("switch", "main-audyssey_dynamic_eq", "audyssey_dynamic_eq"),
    ("switch", "main-music_optimizer", "music_optimizer"),
    ("switch", "main-trigger_a", "trigger_a"),
    ("switch", "main-trigger_b", "trigger_b"),
    ("switch", "main-trigger_c", "trigger_c"),
)

STALE_ENTITY_UNIQUE_IDS: tuple[tuple[str, str], ...] = (
    ("switch", "zone2-power"),
    ("switch", "zone3-power"),
    ("switch", "zone2-mute"),
    ("switch", "zone3-mute"),
)

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Optional(CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=200)
        ),
        vol.Optional(CONF_SOURCES): {
            cv.string: cv.string,
        },
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [DEVICE_SCHEMA])},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import YAML config into config entries."""
    hass.data.setdefault(DOMAIN, {})
    await _async_register_services(hass)

    devices = config.get(DOMAIN, [])
    for device_config in devices:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=device_config,
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Onkyo from a config entry."""
    model = str(entry.data.get(CONF_MODEL, DEFAULT_MODEL)).strip().upper()
    runtime = build_runtime_data(
        hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        name=entry.data.get(CONF_NAME)
        or PROFILE_DEFAULT_NAMES[model],
        model=model,
        scan_interval=entry.data[CONF_SCAN_INTERVAL],
        sources=entry.data.get(CONF_SOURCES) or PROFILE_DEFAULT_SOURCES[model],
        max_volume=entry.data[CONF_MAX_VOLUME],
    )

    for command in runtime.queryable_commands:
        runtime.coordinator.set_command_capability(command, True)

    hass.async_create_task(_async_initial_refresh(runtime))

    runtime.zones = (runtime, *runtime.candidate_zones)

    hass.data[DOMAIN][entry.entry_id] = runtime
    await _async_migrate_main_entity_unique_ids(hass, runtime.host)
    await _async_remove_stale_entity_registry_entries(hass, runtime.host)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.bus.async_listen_once("homeassistant_stop", lambda _event: _disconnect_all(hass))
    return True


async def _async_initial_refresh(runtime: OnkyoRuntimeData) -> None:
    """Perform a best-effort first refresh without blocking setup."""
    try:
        await runtime.coordinator.async_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Initial state refresh failed for %s:%s: %s",
            runtime.host,
            runtime.port,
            err,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Onkyo config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        runtime.coordinator.client.disconnect()
    return unload_ok


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def async_handle_refresh(call: ServiceCall) -> None:
        runtime = _resolve_runtime(hass, call.data.get(CONF_ENTITY_ID))
        for zone_runtime in getattr(runtime, "zones", (runtime,)):
            await zone_runtime.coordinator.async_request_refresh()

    async def async_handle_set_listening_mode(call: ServiceCall) -> None:
        runtime = _resolve_runtime(hass, call.data.get(CONF_ENTITY_ID))
        mode = str(call.data[ATTR_LISTENING_MODE]).lower()
        if mode not in runtime.supported_listening_modes:
            raise vol.Invalid(f"Unsupported listening mode: {mode}")
        await runtime.coordinator.async_set_listening_mode(mode)

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        async_handle_refresh,
        schema=vol.Schema({vol.Optional(CONF_ENTITY_ID): cv.entity_id}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_LISTENING_MODE,
        async_handle_set_listening_mode,
        schema=vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID): cv.entity_id,
                vol.Required(ATTR_LISTENING_MODE): cv.string,
            }
        ),
    )


def _resolve_runtime(hass: HomeAssistant, entity_id: str | None) -> OnkyoRuntimeData:
    runtimes: dict[str, OnkyoRuntimeData] = hass.data[DOMAIN]
    if entity_id:
        for runtime in runtimes.values():
            if entity_id in runtime.entity_ids:
                return runtime
        raise vol.Invalid(f"Unknown Onkyo Legacy entity_id: {entity_id}")

    if len(runtimes) == 1:
        return next(iter(runtimes.values()))

    raise vol.Invalid("entity_id is required when multiple Onkyo Legacy devices are configured")


def _disconnect_all(hass: HomeAssistant) -> None:
    for runtime in hass.data.get(DOMAIN, {}).values():
        runtime.coordinator.client.disconnect()


async def _async_migrate_main_entity_unique_ids(hass: HomeAssistant, host: str) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for platform, current_suffix, legacy_suffix in MAIN_ENTITY_UNIQUE_ID_MIGRATIONS:
        current_unique_id = f"{host}-{current_suffix}"
        legacy_unique_id = f"{host}-{legacy_suffix}"
        current_entity_id = registry.async_get_entity_id(platform, DOMAIN, current_unique_id)
        legacy_entity_id = registry.async_get_entity_id(platform, DOMAIN, legacy_unique_id)

        if current_entity_id and legacy_entity_id:
            registry.async_remove(legacy_entity_id)
            continue

        if legacy_entity_id and not current_entity_id:
            registry.async_update_entity(legacy_entity_id, new_unique_id=current_unique_id)
            current_entity_id = registry.async_get_entity_id(platform, DOMAIN, current_unique_id)
        else:
            current_entity_id = registry.async_get_entity_id(platform, DOMAIN, current_unique_id)

        if current_entity_id and current_entity_id.endswith("_2"):
            desired_entity_id = current_entity_id[:-2]
            _async_update_entity_id_if_available(registry, current_entity_id, desired_entity_id)


async def _async_remove_stale_entity_registry_entries(hass: HomeAssistant, host: str) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for platform, suffix in STALE_ENTITY_UNIQUE_IDS:
        unique_id = f"{host}-{suffix}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)


def _async_update_entity_id_if_available(registry: object, current_entity_id: str, desired_entity_id: str) -> None:
    async_get = getattr(registry, "async_get", None)
    if callable(async_get) and async_get(desired_entity_id) is not None:
        return

    try:
        registry.async_update_entity(current_entity_id, new_entity_id=desired_entity_id)
    except TypeError:
        return
