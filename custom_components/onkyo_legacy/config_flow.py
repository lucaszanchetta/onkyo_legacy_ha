"""Config flow for Onkyo Legacy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from eiscp import eISCP

from .const import (
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
    MODEL,
    MODEL_TX8050,
    PROFILES,
)

__all__ = ["OnkyoLegacyConfigFlow"]


def _validate_connection(host: str, port: int) -> None:
    """Validate connection to an Onkyo device by sending a power query.

    Runs in an executor to avoid blocking the event loop.
    """
    device = eISCP(host, port)
    try:
        device.raw("!1PWRQSTN")
    finally:
        device.disconnect()


class OnkyoLegacyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Onkyo Legacy config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user setup step via the UI."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input.get(CONF_PORT, DEFAULT_PORT)
            model: str = str(user_input.get(CONF_MODEL, DEFAULT_MODEL)).strip().upper()
            profile = PROFILES.get(model, GENERIC_PROFILE)

            unique_id = f"{host}:{port}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            try:
                await self.hass.async_add_executor_job(
                    _validate_connection, host, port
                )
            except (OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_NAME: profile.default_name,
                    CONF_MODEL: model,
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    CONF_MAX_VOLUME: DEFAULT_MAX_VOLUME,
                    CONF_SOURCES: profile.default_sources,
                    CONF_RETRIES: DEFAULT_RETRIES,
                    CONF_STRICT_SOURCES: DEFAULT_STRICT_SOURCES,
                }
                return self.async_create_entry(
                    title=data[CONF_NAME],
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): cv.string,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): vol.In(
                        [MODEL, MODEL_TX8050]
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration of host/port for an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input.get(
                CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)
            )

            try:
                await self.hass.async_add_executor_job(
                    _validate_connection, host, port
                )
            except (OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: host,
                        CONF_PORT: port,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): cv.string,
                    vol.Optional(
                        CONF_PORT,
                        default=entry.data.get(CONF_PORT, DEFAULT_PORT),
                    ): cv.port,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return OnkyoLegacyOptionsFlowHandler(config_entry)

    async def async_step_import(self, user_input):
        """Import from YAML."""
        model = str(user_input.get(CONF_MODEL, DEFAULT_MODEL)).strip().upper()
        profile = PROFILES.get(model, GENERIC_PROFILE)
        unique_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_HOST: user_input[CONF_HOST],
            CONF_PORT: user_input.get(CONF_PORT, DEFAULT_PORT),
            CONF_NAME: user_input.get(CONF_NAME) or profile.default_name,
            CONF_MODEL: model,
            CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            CONF_MAX_VOLUME: user_input.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME),
            CONF_SOURCES: user_input.get(CONF_SOURCES) or profile.default_sources,
            CONF_RETRIES: user_input.get(CONF_RETRIES, DEFAULT_RETRIES),
            CONF_STRICT_SOURCES: user_input.get(CONF_STRICT_SOURCES, DEFAULT_STRICT_SOURCES),
        }
        return self.async_create_entry(title=data[CONF_NAME], data=data)


class OnkyoLegacyOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Onkyo Legacy options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the runtime options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Optional(
                    CONF_MAX_VOLUME, default=DEFAULT_MAX_VOLUME
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=200)),
                vol.Optional(
                    CONF_RETRIES, default=DEFAULT_RETRIES
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
                vol.Optional(
                    CONF_STRICT_SOURCES, default=DEFAULT_STRICT_SOURCES
                ): cv.boolean,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
