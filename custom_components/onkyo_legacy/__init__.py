"""YAML-configured custom integration for legacy Onkyo processors."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_LISTENING_MODE,
    CONF_MAX_VOLUME,
    CONF_SCAN_INTERVAL,
    CONF_SOURCES,
    DEFAULT_MAX_VOLUME,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SOURCES,
    DOMAIN,
    PLATFORMS,
    SERVICE_REFRESH,
    SERVICE_SET_LISTENING_MODE,
)
from .coordinator import OnkyoRuntimeData, build_runtime_data

_LOGGER = logging.getLogger(__name__)

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=1)
        ),
        vol.Optional(CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=200)
        ),
        vol.Optional(CONF_SOURCES, default=DEFAULT_SOURCES): {
            cv.string: cv.string,
        },
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [DEVICE_SCHEMA])},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up configured legacy Onkyo devices."""
    devices = config.get(DOMAIN)
    if not devices:
        return True

    hass.data.setdefault(DOMAIN, {})

    for index, device_config in enumerate(devices):
        runtime_id = f"{device_config[CONF_HOST]}:{device_config[CONF_PORT]}:{index}"
        runtime = build_runtime_data(
            hass,
            host=device_config[CONF_HOST],
            port=device_config[CONF_PORT],
            name=device_config[CONF_NAME],
            scan_interval=device_config[CONF_SCAN_INTERVAL],
            sources=device_config[CONF_SOURCES],
            max_volume=device_config[CONF_MAX_VOLUME],
        )
        try:
            await runtime.coordinator.async_refresh()
        except Exception as err:
            _LOGGER.error(
                "Failed to reach Onkyo device %s:%s: %s",
                device_config[CONF_HOST],
                device_config[CONF_PORT],
                err,
            )
            continue

        if not runtime.coordinator.last_update_success:
            _LOGGER.error(
                "Initial state refresh failed for %s:%s",
                device_config[CONF_HOST],
                device_config[CONF_PORT],
            )
            continue

        runtime.coordinator.supports_listening_mode = await runtime.coordinator._async_probe_optional("LMD")
        runtime.coordinator.supports_speaker_a = await runtime.coordinator._async_probe_optional("SPA")
        runtime.coordinator.supports_speaker_b = await runtime.coordinator._async_probe_optional("SPB")
        runtime.coordinator.supports_trigger_a = await runtime.coordinator._async_probe_optional("TGA")
        runtime.coordinator.supports_trigger_b = await runtime.coordinator._async_probe_optional("TGB")
        runtime.coordinator.supports_trigger_c = await runtime.coordinator._async_probe_optional("TGC")
        runtime.coordinator.supports_dimmer = await runtime.coordinator._async_probe_optional("DIM")

        hass.data[DOMAIN][runtime_id] = runtime
        for platform in PLATFORMS:
            hass.async_create_task(
                async_load_platform(
                    hass,
                    platform,
                    DOMAIN,
                    {"runtime_id": runtime_id},
                    config,
                )
            )

    await _async_register_services(hass)
    hass.bus.async_listen_once("homeassistant_stop", lambda _event: _disconnect_all(hass))
    return True


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def async_handle_refresh(call: ServiceCall) -> None:
        runtime = _resolve_runtime(hass, call.data.get(CONF_ENTITY_ID))
        await runtime.coordinator.async_request_refresh()

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
