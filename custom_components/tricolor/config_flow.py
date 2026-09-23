"""Config flow for TriColor Light."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.schema_config_entry_flow import (
    wrapped_entity_config_entry_title,
)

from .const import (
    CONF_CYCLE_MS,
    CONF_GUARD,
    CONF_RESET_CYCLES,
    CONF_SOURCE,
    DEFAULT_CYCLE_MS,
    DEFAULT_GUARD,
    DEFAULT_RESET_CYCLES,
    DOMAIN,
    SOURCE_DOMAINS,
)


def _tuning_schema(
    guard: float = DEFAULT_GUARD,
    reset_cycles: int = DEFAULT_RESET_CYCLES,
    cycle_ms: int = DEFAULT_CYCLE_MS,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_GUARD, default=guard): vol.All(
                vol.Coerce(float), vol.Range(min=0.5, max=60)
            ),
            vol.Required(CONF_RESET_CYCLES, default=reset_cycles): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=20)
            ),
            vol.Required(CONF_CYCLE_MS, default=cycle_ms): vol.All(
                vol.Coerce(int), vol.Range(min=100, max=2000)
            ),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup of one existing tri-color light."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick the source entity and its power-step tuning."""
        if user_input is not None:
            source = user_input[CONF_SOURCE]
            await self.async_set_unique_id(source)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=wrapped_entity_config_entry_title(self.hass, source),
                data={CONF_SOURCE: source},
                options={
                    CONF_GUARD: float(user_input[CONF_GUARD]),
                    CONF_RESET_CYCLES: int(user_input[CONF_RESET_CYCLES]),
                    CONF_CYCLE_MS: int(user_input[CONF_CYCLE_MS]),
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=SOURCE_DOMAINS)
                    ),
                    vol.Required(CONF_GUARD, default=DEFAULT_GUARD): vol.All(
                        vol.Coerce(float), vol.Range(min=0.5, max=60)
                    ),
                    vol.Required(CONF_RESET_CYCLES, default=DEFAULT_RESET_CYCLES): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=20)
                    ),
                    vol.Required(CONF_CYCLE_MS, default=DEFAULT_CYCLE_MS): vol.All(
                        vol.Coerce(int), vol.Range(min=100, max=2000)
                    ),
                }
            ),
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Retune the power-step timing after setup (reloads automatically)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the tuning options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_GUARD: float(user_input[CONF_GUARD]),
                    CONF_RESET_CYCLES: int(user_input[CONF_RESET_CYCLES]),
                    CONF_CYCLE_MS: int(user_input[CONF_CYCLE_MS]),
                }
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_tuning_schema(
                guard=float(options.get(CONF_GUARD, DEFAULT_GUARD)),
                reset_cycles=int(options.get(CONF_RESET_CYCLES, DEFAULT_RESET_CYCLES)),
                cycle_ms=int(options.get(CONF_CYCLE_MS, DEFAULT_CYCLE_MS)),
            ),
        )


async def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    """Return the options flow."""
    return OptionsFlowHandler()
