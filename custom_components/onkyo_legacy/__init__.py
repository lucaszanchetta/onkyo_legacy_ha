"""Config-entry based integration for legacy Onkyo processors."""

from __future__ import annotations

import asyncio
import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    ATTR_LISTENING_MODE,
    CONF_MAX_VOLUME,
    CONF_MODEL,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_SOURCES,
    CONF_STRICT_SOURCES,
    DEFAULT_MAX_VOLUME,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    DEFAULT_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STRICT_SOURCES,
    DOMAIN,
    GENERIC_PROFILE,
    PLATFORMS,
    PROFILES,
    SERVICE_REFRESH,
    SERVICE_SET_DIMMER,
    SERVICE_SET_LISTENING_MODE,
    SERVICE_SET_SOURCE,
    SERVICE_SET_VOLUME,
)
from .coordinator import (
    ZONE_DEFINITIONS,
    CommandResponseTimeout,
    OnkyoRuntimeData,
    OnkyoZoneRuntimeData,
    build_runtime_data,
)

__all__ = ["async_setup", "async_setup_entry", "async_unload_entry"]

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

ZONE_ENTITY_UNIQUE_ID_SUFFIXES: dict[str, tuple[tuple[str, str], ...]] = {
    "zone2": (
        ("media_player", "zone2"),
        ("number", "zone2-volume-level"),
        ("select", "zone2-source"),
        ("select", "zone2-listening-mode"),
        ("switch", "zone2-power"),
        ("switch", "zone2-mute"),
    ),
    "zone3": (
        ("media_player", "zone3"),
        ("number", "zone3-volume-level"),
        ("select", "zone3-source"),
        ("select", "zone3-listening-mode"),
        ("switch", "zone3-power"),
        ("switch", "zone3-mute"),
    ),
}

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
        vol.Optional(CONF_RETRIES, default=DEFAULT_RETRIES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=5)
        ),
        vol.Optional(CONF_STRICT_SOURCES, default=DEFAULT_STRICT_SOURCES): cv.boolean,
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
    hass.data.setdefault(DOMAIN, {})
    model = str(entry.data.get(CONF_MODEL, DEFAULT_MODEL)).strip().upper()
    profile = PROFILES.get(model, GENERIC_PROFILE)
    runtime = build_runtime_data(
        hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        name=entry.data.get(CONF_NAME) or profile.default_name,
        model=model,
        scan_interval=entry.data[CONF_SCAN_INTERVAL],
        sources=entry.data.get(CONF_SOURCES) or profile.default_sources,
        max_volume=entry.data[CONF_MAX_VOLUME],
        retries=entry.data.get(CONF_RETRIES, DEFAULT_RETRIES),
        strict_sources=entry.data.get(CONF_STRICT_SOURCES, DEFAULT_STRICT_SOURCES),
    )
    runtime.model = model
    runtime.stopping = False
    runtime.platform_adders = getattr(runtime, "platform_adders", {})
    runtime.platform_unique_ids = getattr(runtime, "platform_unique_ids", {})
    hass.data[DOMAIN][entry.entry_id] = runtime
    try:
        if model in PROFILES:
            # Known model — trust the hard-coded command list.
            for command in runtime.queryable_commands:
                runtime.coordinator.set_command_capability(command, True)
        # Unknown zones and generic capabilities remain pending until the
        # background discovery loop can query them without delaying setup.
        runtime.zones = (runtime,)
        runtime.discovery_states = {
            zone.zone_key: "pending" for zone in runtime.candidate_zones
        }
        _propagate_zone_command_capabilities(runtime)
        runtime.initial_refresh_task = hass.async_create_task(
            _async_initial_refresh(runtime)
        )

        def _dispatch_push(message: str) -> None:
            for zone_runtime in runtime.zones:
                zone_runtime.coordinator._handle_push_message(message)

        runtime.coordinator.client.start_listener(_dispatch_push)
        await _async_migrate_main_entity_unique_ids(hass, runtime.host)
        await _async_remove_unsupported_zone_registry_entries(hass, runtime)
        await _async_remove_stale_entity_registry_entries(hass, runtime.host)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        for zone_runtime in runtime.zones:
            start_polling = getattr(
                zone_runtime.coordinator, "async_start_periodic_refresh", None
            )
            if start_polling is not None:
                start_polling()
        _start_discovery_task(hass, runtime)

        async def _async_homeassistant_stop(_event) -> None:
            await _disconnect_all(hass)

        runtime.stop_listener_unsub = hass.bus.async_listen_once(
            "homeassistant_stop", _async_homeassistant_stop
        )
        return True
    except BaseException:
        try:
            await _async_cleanup_runtime(hass, runtime)
        finally:
            hass.data[DOMAIN].pop(entry.entry_id, None)
        raise


async def _async_initial_refresh(runtime: OnkyoRuntimeData) -> None:
    """Perform a best-effort first refresh without blocking setup."""
    try:
        await runtime.coordinator.async_refresh()
    except (UpdateFailed, OSError, ConnectionError) as err:
        _LOGGER.warning(
            "Initial state refresh failed for %s:%s: %s",
            runtime.host,
            runtime.port,
            err,
        )


async def _async_cleanup_runtime(
    hass: HomeAssistant, runtime: OnkyoRuntimeData
) -> None:
    """Finish listener, refresh-task, and socket cleanup despite cancellation."""
    client = runtime.coordinator.client
    runtime.stopping = True
    client.begin_close()
    zone_runtimes = getattr(runtime, "zones", ()) or (runtime,)
    for zone_runtime in zone_runtimes:
        zone_runtime.coordinator._closing = True

    async def _cleanup() -> None:
        stop_unsub = getattr(runtime, "stop_listener_unsub", None)
        if stop_unsub:
            stop_unsub()
        initial_task = getattr(runtime, "initial_refresh_task", None)
        if initial_task:
            initial_task.cancel()
            await asyncio.gather(initial_task, return_exceptions=True)
        discovery_task = getattr(runtime, "discovery_task", None)
        if discovery_task:
            discovery_task.cancel()
            await asyncio.gather(discovery_task, return_exceptions=True)
        for zone_runtime in zone_runtimes:
            cancel_optional = getattr(
                zone_runtime.coordinator, "async_cancel_optional_refresh", None
            )
            if cancel_optional is not None:
                await cancel_optional()
        await hass.async_add_executor_job(client.close)

    cleanup_task = asyncio.create_task(_cleanup())
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _async_discovery_loop(runtime: OnkyoRuntimeData) -> None:
    """Discover zone presence and generic commands without blocking setup."""
    zone_cursor = 0
    generic_commands = tuple(
        command
        for command in runtime.queryable_commands
        if command not in {"PWR", "MVL", "AMT", "SLI"}
    )
    try:
        while not getattr(runtime, "stopping", False):
            capabilities = getattr(runtime.coordinator, "command_capabilities", {})
            candidates = tuple(
                zone
                for zone in runtime.candidate_zones
                if runtime.discovery_states.get(zone.zone_key)
                not in {"supported", "unsupported"}
            )
            generic_pending = tuple(
                command
                for command in generic_commands
                if command not in capabilities
            )
            if not candidates and not generic_pending:
                return

            cooldown = runtime.coordinator.client.cooldown_remaining()
            if cooldown > 0:
                await asyncio.sleep(cooldown)
                continue

            if candidates:
                zone_cursor %= len(candidates)
                zone = candidates[zone_cursor]
                try:
                    command = zone.coordinator.zone.power_command
                    response = await runtime.coordinator.hass.async_add_executor_job(
                        runtime.coordinator.client.query_optional, command, 1.5
                    )
                    if runtime.stopping:
                        return
                    if response.startswith(command) and response[len(command) :] in {
                        "00",
                        "01",
                    }:
                        runtime.discovery_states[zone.zone_key] = "supported"
                        if all(item.zone_key != zone.zone_key for item in runtime.zones):
                            runtime.zones += (zone,)
                            start_polling = getattr(
                                zone.coordinator, "async_start_periodic_refresh", None
                            )
                            if start_polling is not None:
                                start_polling()
                            _propagate_zone_command_capabilities(runtime)
                            _notify_dynamic_entities(runtime)
                    else:
                        runtime.discovery_states[zone.zone_key] = "inconclusive"
                except CommandResponseTimeout:
                    if runtime.stopping:
                        return
                    if (
                        runtime.coordinator.core_power_is_fresh
                        and runtime.coordinator.data.power
                    ):
                        count = runtime.discovery_timeouts.get(zone.zone_key, 0) + 1
                        runtime.discovery_timeouts[zone.zone_key] = count
                        runtime.discovery_states[zone.zone_key] = (
                            "unsupported" if count >= 2 else "pending"
                        )
                        if count >= 2:
                            await _async_remove_unsupported_zone_registry_entries(
                                runtime.coordinator.hass, runtime
                            )
                    else:
                        runtime.discovery_states[zone.zone_key] = "inconclusive"
                except (OSError, ConnectionError, TimeoutError, ValueError) as err:
                    if runtime.stopping:
                        return
                    runtime.discovery_states[zone.zone_key] = "inconclusive"
                    _LOGGER.debug(
                        "Zone discovery for %s was inconclusive: %s", zone.zone_key, err
                    )
                finally:
                    zone_cursor = (zone_cursor + 1) % max(
                        1, len(runtime.candidate_zones)
                    )

            cooldown = runtime.coordinator.client.cooldown_remaining()
            if cooldown > 0:
                await asyncio.sleep(cooldown)
                continue

            if (
                runtime.model not in PROFILES
                and runtime.coordinator.core_power_is_fresh
                and runtime.coordinator.data.power
            ):
                pending_commands = [
                    generic_commands[(getattr(runtime, "capability_cursor", 0) + offset) % len(generic_commands)]
                    for offset in range(len(generic_commands))
                    if generic_commands[(getattr(runtime, "capability_cursor", 0) + offset) % len(generic_commands)]
                    not in capabilities
                ][:2]
                for command in pending_commands:
                    if runtime.stopping:
                        return
                    pause_for_cooldown = False
                    try:
                        await runtime.coordinator.hass.async_add_executor_job(
                            runtime.coordinator.client.query_optional, command, 1.5
                        )
                    except CommandResponseTimeout:
                        if runtime.stopping:
                            return
                        if (
                            runtime.coordinator.core_power_is_fresh
                            and runtime.coordinator.data.power
                        ):
                            count = runtime.capability_timeouts.get(command, 0) + 1
                            runtime.capability_timeouts[command] = count
                            if count >= 2:
                                runtime.coordinator.set_command_capability(
                                    command, False
                                )
                    except (OSError, ConnectionError, TimeoutError, ValueError):
                        pass
                    else:
                        if runtime.stopping:
                            return
                        runtime.coordinator.set_command_capability(command, True)
                        _propagate_zone_command_capabilities(runtime)
                        runtime.coordinator._start_optional_refresh()
                        _notify_dynamic_entities(runtime)
                    finally:
                        runtime.capability_cursor = (
                            generic_commands.index(command) + 1
                        ) % max(1, len(generic_commands))
                        pause_for_cooldown = (
                            runtime.coordinator.client.cooldown_remaining() > 0
                        )
                    if pause_for_cooldown:
                        break
            cooldown = runtime.coordinator.client.cooldown_remaining()
            if cooldown > 0:
                await asyncio.sleep(cooldown)
                continue
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise


def _notify_dynamic_entities(runtime: OnkyoRuntimeData) -> None:
    """Refresh registered platform factories after discovery changes."""
    if runtime.stopping:
        return
    for add_entities in tuple(runtime.platform_adders.values()):
        add_entities()


def _start_discovery_task(hass: HomeAssistant, runtime: OnkyoRuntimeData) -> None:
    """Start discovery once when the runtime is active."""
    task = getattr(runtime, "discovery_task", None)
    if runtime.stopping or (task is not None and not task.done()):
        return
    runtime.discovery_task = hass.async_create_background_task(
        _async_discovery_loop(runtime), name=f"Onkyo discovery {runtime.host}"
    )


def _propagate_zone_command_capabilities(runtime: OnkyoRuntimeData) -> None:
    """Propagate relevant command capabilities from main to zone coordinators."""
    for zone_runtime in runtime.zones:
        if zone_runtime.zone_key == "main":
            continue
        zone_def = ZONE_DEFINITIONS.get(zone_runtime.zone_key)
        if zone_def is None:
            continue
        for command in zone_def.optional_commands:
            if runtime.coordinator.supports(command):
                zone_runtime.coordinator.set_command_capability(command, True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an Onkyo config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is not None:
        runtime.stopping = True
        discovery_task = getattr(runtime, "discovery_task", None)
        if discovery_task is not None and not discovery_task.done():
            discovery_task.cancel()
            await asyncio.gather(discovery_task, return_exceptions=True)
        runtime.discovery_task = None
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        if runtime is not None:
            runtime.stopping = False
            _start_discovery_task(hass, runtime)
        return False
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None:
        await _async_cleanup_runtime(hass, runtime)
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

    async def async_handle_set_source(call: ServiceCall) -> None:
        zone_runtime = _resolve_zone_runtime(hass, call.data.get(CONF_ENTITY_ID))
        source = call.data["source"]
        if source not in zone_runtime.sources:
            raise vol.Invalid(
                f"Unknown source: {source}. "
                f"Available: {', '.join(sorted(zone_runtime.sources))}"
            )
        await zone_runtime.coordinator.async_select_source(zone_runtime.sources[source])

    async def async_handle_set_volume(call: ServiceCall) -> None:
        volume = call.data["volume"]
        zone_runtime = _resolve_zone_runtime(hass, call.data.get(CONF_ENTITY_ID))
        max_volume = getattr(zone_runtime, "max_volume", DEFAULT_MAX_VOLUME)
        await zone_runtime.coordinator.async_set_volume(volume / max_volume, max_volume)

    async def async_handle_set_dimmer(call: ServiceCall) -> None:
        zone_runtime = _resolve_zone_runtime(hass, call.data.get(CONF_ENTITY_ID))
        level = call.data["level"]
        await zone_runtime.coordinator.async_set_dimmer_level(level)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SOURCE,
        async_handle_set_source,
        schema=vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID): cv.entity_id,
                vol.Required("source"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_VOLUME,
        async_handle_set_volume,
        schema=vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID): cv.entity_id,
                vol.Required("volume"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=200)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DIMMER,
        async_handle_set_dimmer,
        schema=vol.Schema(
            {
                vol.Optional(CONF_ENTITY_ID): cv.entity_id,
                vol.Required("level"): cv.string,
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

    raise vol.Invalid(
        "entity_id is required when multiple Onkyo Legacy devices are configured"
    )


def _resolve_zone_runtime(
    hass: HomeAssistant, entity_id: str | None
) -> OnkyoRuntimeData | OnkyoZoneRuntimeData:
    """Resolve the zone runtime for a given entity_id.

    Returns the main zone runtime if no entity_id is given or if the zone
    cannot be determined from the entity registry.
    """
    runtime = _resolve_runtime(hass, entity_id)
    if not entity_id:
        return runtime.zones[0]
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None:
        for zone_runtime in runtime.zones:
            prefix = f"{runtime.host}-{zone_runtime.zone_key}"
            if entry.unique_id.startswith(prefix):
                return zone_runtime
    return runtime.zones[0]


async def _disconnect_all(hass: HomeAssistant) -> None:
    runtimes = tuple(hass.data.get(DOMAIN, {}).values())
    for runtime in runtimes:
        await _async_cleanup_runtime(hass, runtime)


async def _async_migrate_main_entity_unique_ids(hass: HomeAssistant, host: str) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for platform, current_suffix, legacy_suffix in MAIN_ENTITY_UNIQUE_ID_MIGRATIONS:
        current_unique_id = f"{host}-{current_suffix}"
        legacy_unique_id = f"{host}-{legacy_suffix}"
        current_entity_id = registry.async_get_entity_id(
            platform, DOMAIN, current_unique_id
        )
        legacy_entity_id = registry.async_get_entity_id(
            platform, DOMAIN, legacy_unique_id
        )

        if current_entity_id and legacy_entity_id:
            registry.async_remove(legacy_entity_id)
            continue

        if legacy_entity_id and not current_entity_id:
            registry.async_update_entity(
                legacy_entity_id, new_unique_id=current_unique_id
            )
            current_entity_id = registry.async_get_entity_id(
                platform, DOMAIN, current_unique_id
            )
        else:
            current_entity_id = registry.async_get_entity_id(
                platform, DOMAIN, current_unique_id
            )

        if current_entity_id and current_entity_id.endswith("_2"):
            desired_entity_id = current_entity_id[:-2]
            _async_update_entity_id_if_available(
                registry, current_entity_id, desired_entity_id
            )


async def _async_remove_stale_entity_registry_entries(
    hass: HomeAssistant, host: str
) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for platform, suffix in STALE_ENTITY_UNIQUE_IDS:
        unique_id = f"{host}-{suffix}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id:
            registry.async_remove(entity_id)


async def _async_remove_unsupported_zone_registry_entries(
    hass: HomeAssistant,
    runtime: OnkyoRuntimeData,
) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    active_zone_keys = {zone_runtime.zone_key for zone_runtime in runtime.zones}
    pending_zone_keys = {
        zone_key
        for zone_key, state in getattr(runtime, "discovery_states", {}).items()
        if state in {"pending", "inconclusive"}
    }
    for zone_key, entries in ZONE_ENTITY_UNIQUE_ID_SUFFIXES.items():
        if zone_key in active_zone_keys or zone_key in pending_zone_keys:
            continue
        for platform, suffix in entries:
            unique_id = f"{runtime.host}-{suffix}"
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if entity_id:
                registry.async_remove(entity_id)


def _async_update_entity_id_if_available(
    registry: object, current_entity_id: str, desired_entity_id: str
) -> None:
    async_get = getattr(registry, "async_get", None)
    if callable(async_get) and async_get(desired_entity_id) is not None:
        return

    try:
        registry.async_update_entity(current_entity_id, new_entity_id=desired_entity_id)
    except TypeError:
        return
