"""Config flow for Onkyo Legacy."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT

from .const import (
    CONF_MODEL,
    CONF_MAX_VOLUME,
    CONF_SCAN_INTERVAL,
    CONF_SOURCES,
    DEFAULT_MODEL,
    DEFAULT_MAX_VOLUME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    PROFILE_DEFAULT_NAMES,
    PROFILE_DEFAULT_SOURCES,
    DOMAIN,
)


class OnkyoLegacyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Onkyo Legacy config flow."""

    VERSION = 1

    async def async_step_import(self, user_input):
        """Import from YAML."""
        model = str(user_input.get(CONF_MODEL, DEFAULT_MODEL)).strip().upper()
        unique_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data = {
            CONF_HOST: user_input[CONF_HOST],
            CONF_PORT: user_input.get(CONF_PORT, DEFAULT_PORT),
            CONF_NAME: user_input.get(CONF_NAME) or PROFILE_DEFAULT_NAMES[model],
            CONF_MODEL: model,
            CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            CONF_MAX_VOLUME: user_input.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME),
            CONF_SOURCES: user_input.get(CONF_SOURCES) or PROFILE_DEFAULT_SOURCES[model],
        }
        return self.async_create_entry(title=data[CONF_NAME], data=data)
