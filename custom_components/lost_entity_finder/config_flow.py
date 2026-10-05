"""Config flow for Lost Entity Finder."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_ENABLE_BULK_FIX,
    CONF_MAX_PENDING_CHANGES,
    DEFAULT_ENABLE_BULK_FIX,
    DEFAULT_MAX_PENDING_CHANGES,
    DOMAIN,
    MAX_MAX_PENDING_CHANGES,
    MIN_MAX_PENDING_CHANGES,
)


class EntityFinderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lost Entity Finder."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Lost Entity Finder",
                data={
                    CONF_ENABLE_BULK_FIX: user_input.get(
                        CONF_ENABLE_BULK_FIX, DEFAULT_ENABLE_BULK_FIX
                    ),
                    CONF_MAX_PENDING_CHANGES: user_input.get(
                        CONF_MAX_PENDING_CHANGES, DEFAULT_MAX_PENDING_CHANGES
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_BULK_FIX, default=DEFAULT_ENABLE_BULK_FIX
                    ): bool,
                    vol.Optional(
                        CONF_MAX_PENDING_CHANGES,
                        default=DEFAULT_MAX_PENDING_CHANGES,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_MAX_PENDING_CHANGES,
                            max=MAX_MAX_PENDING_CHANGES,
                        ),
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EntityFinderOptionsFlow:
        """Get the options flow."""
        return EntityFinderOptionsFlow()


class EntityFinderOptionsFlow(config_entries.OptionsFlow):
    """Handle Lost Entity Finder options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage Lost Entity Finder options."""
        current_bulk_fix = self.config_entry.data.get(
            CONF_ENABLE_BULK_FIX, DEFAULT_ENABLE_BULK_FIX
        )
        current_max_pending = self.config_entry.data.get(
            CONF_MAX_PENDING_CHANGES, DEFAULT_MAX_PENDING_CHANGES
        )
        if self.config_entry.options:
            current_bulk_fix = self.config_entry.options.get(
                CONF_ENABLE_BULK_FIX, current_bulk_fix
            )
            current_max_pending = self.config_entry.options.get(
                CONF_MAX_PENDING_CHANGES, current_max_pending
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            from .entry_helpers import get_loaded_manager

            manager = get_loaded_manager(self.hass)
            turning_off_bulk = (
                not bool(user_input.get(CONF_ENABLE_BULK_FIX, True))
                and bool(current_bulk_fix)
            )
            if (
                manager is not None
                and manager.is_auto_replace_all_running
                and turning_off_bulk
            ):
                errors[CONF_ENABLE_BULK_FIX] = "bulk_fix_running"
            else:
                return self.async_create_entry(title="", data=user_input)
            current_bulk_fix = bool(
                user_input.get(CONF_ENABLE_BULK_FIX, current_bulk_fix)
            )
            current_max_pending = int(
                user_input.get(CONF_MAX_PENDING_CHANGES, current_max_pending)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_ENABLE_BULK_FIX, default=current_bulk_fix): bool,
                    vol.Optional(
                        CONF_MAX_PENDING_CHANGES, default=current_max_pending
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_MAX_PENDING_CHANGES,
                            max=MAX_MAX_PENDING_CHANGES,
                        ),
                    ),
                }
            ),
            errors=errors,
        )


def get_enable_bulk_fix(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Return whether bulk fix is enabled for the config entry."""
    if entry.options and CONF_ENABLE_BULK_FIX in entry.options:
        return bool(entry.options[CONF_ENABLE_BULK_FIX])
    return bool(entry.data.get(CONF_ENABLE_BULK_FIX, DEFAULT_ENABLE_BULK_FIX))


def get_max_pending_changes(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> int:
    """Return the pending entity ID change cap for the config entry."""
    if entry.options and CONF_MAX_PENDING_CHANGES in entry.options:
        return int(entry.options[CONF_MAX_PENDING_CHANGES])
    return int(
        entry.data.get(CONF_MAX_PENDING_CHANGES, DEFAULT_MAX_PENDING_CHANGES)
    )
