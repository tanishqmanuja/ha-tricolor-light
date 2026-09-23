"""Config flow for TriColor Light."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.schema_config_entry_flow import (
    wrapped_entity_config_entry_title,
)

from .const import (
    CONF_CYCLE_MS,
    CONF_GUARD,
    CONF_LIGHTS,
    CONF_RESET_CYCLES,
    CONF_SOURCE,
    DEFAULT_CYCLE_MS,
    DEFAULT_GUARD,
    DEFAULT_RESET_CYCLES,
    DOMAIN,
    SOURCE_DOMAINS,
)


def _key_for(source: str, taken: set[str]) -> str:
    """Derive a lights-dict key from a source entity id, deduplicated."""
    base = source.split(".", 1)[1].replace("-", "_") if "." in source else source
    key, n = base, 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    return key


def _configured_sources(hass) -> set[str]:
    """Collect sources already wrapped by any TriColor entry."""
    found: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        for light in (entry.data.get(CONF_LIGHTS, {}) or {}).values():
            if source := light.get(CONF_SOURCE):
                found.add(str(source))
    return found


def _entry_title(hass, sources: list[str]) -> str:
    """Name an entry after its device, its light, or its light count."""
    if len(sources) == 1:
        return wrapped_entity_config_entry_title(hass, sources[0])
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    names: set[str] = set()
    for source in sources:
        wrapped = registry.async_get(source)
        if (
            wrapped
            and wrapped.device_id
            and (device := devices.async_get(wrapped.device_id))
            and (name := device.name_by_user or device.name)
        ):
            names.add(name)
    if len(names) == 1:
        return names.pop()
    return f"TriColor Lights ({len(sources)})"


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


def _defaults(source: str) -> dict[str, Any]:
    return {
        CONF_SOURCE: source,
        CONF_GUARD: DEFAULT_GUARD,
        CONF_RESET_CYCLES: DEFAULT_RESET_CYCLES,
        CONF_CYCLE_MS: DEFAULT_CYCLE_MS,
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup of one or more tri-color lights in a single entry."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick the lights; tuning lives in the entry options afterwards."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            picked: list[str] = user_input.get(CONF_LIGHTS) or []
            dupes = sorted({s for s in picked if s in _configured_sources(self.hass)})
            if not picked:
                errors["base"] = "no_lights"
            elif dupes:
                errors["base"] = "already_configured"
                placeholders["sources"] = ", ".join(dupes)
            else:
                lights: dict[str, dict[str, Any]] = {}
                for source in picked:
                    lights[_key_for(source, set(lights))] = _defaults(source)
                return self.async_create_entry(
                    title=_entry_title(self.hass, picked),
                    data={CONF_LIGHTS: lights},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHTS): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=SOURCE_DOMAINS, multiple=True
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Tune, add, or remove lights of an entry (saving reloads it)."""

    def _lights(self) -> dict[str, dict[str, Any]]:
        return {
            str(key): dict(value)
            for key, value in self.config_entry.data.get(CONF_LIGHTS, {}).items()
        }

    def _save(self, lights: dict[str, dict[str, Any]]) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={CONF_LIGHTS: lights}
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Menu of what to change."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["tune", "add", "remove"],
        )

    async def async_step_tune(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick which light to tune."""
        lights = self._lights()
        if user_input is not None:
            self._tune_key = user_input["light"]
            return await self.async_step_tune_values()
        return self.async_show_form(
            step_id="tune",
            data_schema=vol.Schema(
                {
                    vol.Required("light"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=sorted(lights), mode="dropdown"
                        )
                    )
                }
            ),
        )

    async def async_step_tune_values(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Tune one light."""
        lights = self._lights()
        current = lights[self._tune_key]
        if user_input is not None:
            lights[self._tune_key] = {
                **current,
                CONF_GUARD: float(user_input[CONF_GUARD]),
                CONF_RESET_CYCLES: int(user_input[CONF_RESET_CYCLES]),
                CONF_CYCLE_MS: int(user_input[CONF_CYCLE_MS]),
            }
            self._save(lights)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="tune_values",
            data_schema=_tuning_schema(
                guard=float(current.get(CONF_GUARD, DEFAULT_GUARD)),
                reset_cycles=int(
                    current.get(CONF_RESET_CYCLES, DEFAULT_RESET_CYCLES)
                ),
                cycle_ms=int(current.get(CONF_CYCLE_MS, DEFAULT_CYCLE_MS)),
            ),
            description_placeholders={"light": str(current.get(CONF_SOURCE, ""))},
        )

    async def async_step_add(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Add more lights with default tuning."""
        if user_input is not None:
            picked: list[str] = user_input.get(CONF_LIGHTS) or []
            lights = self._lights()
            have = {light[CONF_SOURCE] for light in lights.values()}
            taken = _configured_sources(self.hass) - have
            for source in picked:
                if source not in have and source not in taken:
                    lights[_key_for(source, set(lights))] = _defaults(source)
            self._save(lights)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="add",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHTS): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=SOURCE_DOMAINS, multiple=True
                        )
                    ),
                }
            ),
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Drop lights from the entry (at least one must stay)."""
        lights = self._lights()
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = set(user_input.get(CONF_LIGHTS, []))
            if chosen and chosen >= set(lights):
                errors["base"] = "cannot_remove_last"
            else:
                for key in chosen:
                    lights.pop(key, None)
                self._save(lights)
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIGHTS, default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=sorted(lights), multiple=True
                        )
                    )
                }
            ),
            errors=errors,
        )


async def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    """Return the options flow."""
    return OptionsFlowHandler()
