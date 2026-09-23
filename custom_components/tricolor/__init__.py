"""TriColor Light integration.

One config entry wraps one or more existing ``light`` entities driving Philips
UltraGlow tunable-white (tri-color) drivers. Each wrapped light gets a
``<name> TriColor`` color-temperature ``light`` entity on the same device.

Hardware model (per the driver datasheet): a fast OFF -> ON power step (within
~2 s) advances the color ``cool -> natural -> warm -> cool``; leaving the light
OFF for longer than ~5 s retains the current color; 7 quick toggles (starting
from ON) reset the driver to cool daylight (6500 K). This integration keeps the
known mode in memory, spaces power steps through a guard window so normal
on/off use never cycles the color, and runs the stepping procedure whenever a
different mode is requested.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.helper_integration import async_handle_source_entity_changes
from homeassistant.helpers.storage import Store

from .config_flow import _key_for

from .const import (
    ATTR_MODE,
    CONF_CYCLE_MS,
    CONF_GUARD,
    CONF_LIGHTS,
    CONF_RESET_CYCLES,
    CONF_SOURCE,
    CYCLE_ORDER,
    DEFAULT_CYCLE_MS,
    DEFAULT_GUARD,
    DEFAULT_MODE,
    DEFAULT_RESET_CYCLES,
    DOMAIN,
    MODES,
    SERVICE_RESET_MODE,
    SERVICE_SET_INTERNAL_STATE,
    SERVICE_SET_MODE,
    SETTLE_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]

SERVICE_BASE_SCHEMA = vol.Schema({vol.Required(CONF_ENTITY_ID): cv.entity_ids})


def _mode_in(value: Any) -> str:
    """Validate a tricolor mode, accepting any letter case."""
    mode = str(value).lower()
    if mode not in MODES:
        raise vol.Invalid(f"Expected one of {', '.join(MODES)} (any case)")
    return mode


SERVICE_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_MODE): _mode_in,
    }
)


def _valid_mode(mode: Any) -> str:
    """Coerce stored/foreign values to a known mode, defaulting safely."""
    mode = str(mode).lower()
    return mode if mode in MODES else DEFAULT_MODE


def _display_mode(mode: str) -> str:
    """Title-case mode for dashboards (Warm / Natural / Cool)."""
    return _valid_mode(mode).title()


class TriColorController:
    """Guarded power-step driver for one wrapped source entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        source_entity_id: str,
        guard: float,
        reset_cycles: int,
        cycle_ms: int,
        mode: str,
    ) -> None:
        """Create a controller; memory is loaded by the setup routine."""
        self.hass = hass
        self.entry_id = entry_id
        self.source_entity_id = source_entity_id
        self.guard = guard
        self.reset_cycles = reset_cycles
        self.cycle_ms = cycle_ms
        self.mode = _valid_mode(mode)
        # Monotonic guard clock. It restarts at "now" on every boot, so the
        # first ON after a restart always waits out one safe guard window.
        self.last_power_change = time.monotonic()
        self.pending = False
        self._lock = asyncio.Lock()
        self._refresh_callbacks: list[callback] = []
        self.tricolor_entity_id: str | None = None

    def subscribe_refresh(self, refresh: callback) -> callback:
        """Register an entity refresh callback; returns an unsubscribe fn."""
        self._refresh_callbacks.append(refresh)

        @callback
        def _unsub() -> None:
            if refresh in self._refresh_callbacks:
                self._refresh_callbacks.remove(refresh)

        return _unsub

    @callback
    def _refresh(self) -> None:
        for refresh in list(self._refresh_callbacks):
            refresh()

    @callback
    def handle_source_state_change(self, event: Event[EventStateChangedData]) -> None:
        """Move the guard clock on power transitions; ignore attribute edits."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or old_state is None:
            return
        if new_state.state != old_state.state:
            self.last_power_change = time.monotonic()
            hass_data = self.hass.data[DOMAIN]
            hass_data["dirty"] = True
            self.hass.async_create_task(_async_save(self.hass))
        self._refresh()

    def _guard_wait(self) -> float:
        """Remaining guard time (seconds) before an ON is safe."""
        return max(0.0, self.guard - (time.monotonic() - self.last_power_change))

    async def _async_call_source(self, turn_on: bool) -> None:
        """Drive the wrapped light through the domain-agnostic service."""
        await self.hass.services.async_call(
            "homeassistant",
            "turn_on" if turn_on else "turn_off",
            {"entity_id": self.source_entity_id},
            blocking=True,
        )
        self.last_power_change = time.monotonic()
        self.hass.data[DOMAIN]["dirty"] = True
        await _async_save(self.hass)

    async def async_turn_on_guarded(self) -> None:
        """Turn on, waiting out the guard window so the color is retained."""
        async with self._lock:
            wait = self._guard_wait()
            if wait > 0:
                self.pending = True
                self._refresh()
                try:
                    await asyncio.sleep(wait)
                finally:
                    self.pending = False
                    self._refresh()
            await self._async_call_source(turn_on=True)
            self._refresh()

    async def async_turn_off(self) -> None:
        """Turn off. Cutting power never cycles the driver color."""
        async with self._lock:
            await self._async_call_source(turn_on=False)
            self._refresh()

    async def async_set_mode(self, mode: str) -> None:
        """Step the hardware forward to ``mode`` following the cycle order."""
        mode = _valid_mode(mode)
        async with self._lock:
            state = self.hass.states.get(self.source_entity_id)
            is_on = state is not None and state.state == "on"
            if is_on and self.mode == mode:
                _LOGGER.debug("%s already %s, skipping", self.source_entity_id, mode)
                return
            if not is_on:
                # Power on first, spaced past the guard so this ON retains the
                # color instead of stepping the driver, then let the relay
                # settle before cycling.
                wait = self._guard_wait()
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._async_call_source(turn_on=True)
                await asyncio.sleep(SETTLE_SECONDS)
            steps = (CYCLE_ORDER.index(mode) - CYCLE_ORDER.index(self.mode)) % len(
                CYCLE_ORDER
            )
            pause = self.cycle_ms / 1000
            for _ in range(steps):
                await self._async_call_source(turn_on=False)
                await asyncio.sleep(pause)
                await self._async_call_source(turn_on=True)
                await asyncio.sleep(pause)
            self.mode = mode
            self.hass.data[DOMAIN]["dirty"] = True
            await _async_save(self.hass)
            self._refresh()

    async def async_reset_mode(self) -> None:
        """Run the full hardware reset procedure (-> cool daylight).

        The driver requires the light to be ON first; this always runs all
        ``reset_cycles`` steps even if memory already says cool.
        """
        async with self._lock:
            state = self.hass.states.get(self.source_entity_id)
            if state is None or state.state != "on":
                wait = self._guard_wait()
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._async_call_source(turn_on=True)
                await asyncio.sleep(SETTLE_SECONDS)
            pause = self.cycle_ms / 1000
            for _ in range(self.reset_cycles):
                await self._async_call_source(turn_on=False)
                await asyncio.sleep(pause)
                await self._async_call_source(turn_on=True)
                await asyncio.sleep(pause)
            self.mode = CYCLE_ORDER[0]
            self.hass.data[DOMAIN]["dirty"] = True
            await _async_save(self.hass)
            self._refresh()

    async def async_set_internal_state(self, mode: str) -> None:
        """Correct the remembered mode WITHOUT touching the hardware.

        Recovery path for manual desyncs (e.g. someone flipped a wall switch
        quickly outside of Home Assistant).
        """
        async with self._lock:
            self.mode = _valid_mode(mode)
            self.hass.data[DOMAIN]["dirty"] = True
            await _async_save(self.hass)
            self._refresh()


def _get_store(hass: HomeAssistant) -> Store:
    data = hass.data.setdefault(
        DOMAIN,
        {
            "controllers": {},
            "entity_map": {},
            "memory": {},
            "services": False,
            "dirty": False,
            "save_lock": asyncio.Lock(),
        },
    )
    if "store" not in data:
        data["store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    return data["store"]


async def _async_save(hass: HomeAssistant) -> None:
    """Persist remembered modes (debounced via the dirty flag + lock)."""
    data = hass.data[DOMAIN]
    if not data.get("dirty"):
        return
    async with data["save_lock"]:
        if not data.get("dirty"):
            return
        data["dirty"] = False
        store: Store = data["store"]
        memory = {
            controller.source_entity_id: {"mode": controller.mode}
            for entry_controllers in data["controllers"].values()
            for controller in entry_controllers.values()
        }
        data["memory"] = memory
        await store.async_save({"modes": memory})


def _resolve_controllers(
    hass: HomeAssistant, entity_ids: list[str]
) -> list[TriColorController]:
    """Map TriColor entity ids to their controllers.

    Unknown ids raise a service error.
    """
    data = hass.data[DOMAIN]
    registry = er.async_get(hass)
    resolved: list[TriColorController] = []
    unknown: list[str] = []
    for entity_id in entity_ids:
        controller: TriColorController | None = data["entity_map"].get(entity_id)
        if controller is None and (entry := registry.async_get(entity_id)) is not None:
            for candidate in data["controllers"].get(entry.config_entry_id, {}).values():
                if candidate.tricolor_entity_id == entity_id:
                    controller = candidate
                    break
        if controller is None:
            unknown.append(entity_id)
        else:
            resolved.append(controller)
    if unknown:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_tricolor",
            translation_placeholders={"entities": ", ".join(unknown)},
        )
    return resolved


async def _handle_set_mode(call: ServiceCall) -> None:
    for controller in _resolve_controllers(call.hass, call.data[CONF_ENTITY_ID]):
        await controller.async_set_mode(str(call.data[ATTR_MODE]).lower())


async def _handle_reset_mode(call: ServiceCall) -> None:
    for controller in _resolve_controllers(call.hass, call.data[CONF_ENTITY_ID]):
        await controller.async_reset_mode()


async def _handle_set_internal_state(call: ServiceCall) -> None:
    for controller in _resolve_controllers(call.hass, call.data[CONF_ENTITY_ID]):
        await controller.async_set_internal_state(str(call.data[ATTR_MODE]).lower())


def _register_services(hass: HomeAssistant) -> None:
    data = hass.data[DOMAIN]
    if data["services"]:
        return
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MODE, _handle_set_mode, SERVICE_MODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_MODE, _handle_reset_mode, SERVICE_BASE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_INTERNAL_STATE,
        _handle_set_internal_state,
        SERVICE_MODE_SCHEMA,
    )
    data["services"] = True


def _unregister_services(hass: HomeAssistant) -> None:
    data = hass.data.get(DOMAIN)
    if not data or not data.get("services"):
        return
    for service in (SERVICE_SET_MODE, SERVICE_RESET_MODE, SERVICE_SET_INTERNAL_STATE):
        hass.services.async_remove(DOMAIN, service)
    data["services"] = False


def _entry_lights(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Normalize one entry's lights to {key: {source, guard, cycles, ms}}."""
    lights: dict[str, dict[str, Any]] = {}
    for key, item in (entry.data.get(CONF_LIGHTS, {}) or {}).items():
        item = dict(item)
        source = str(item.get(CONF_SOURCE, ""))
        if not source:
            continue
        lights[str(key)] = {
            CONF_SOURCE: source,
            CONF_GUARD: float(item.get(CONF_GUARD, DEFAULT_GUARD)),
            CONF_RESET_CYCLES: int(
                item.get(CONF_RESET_CYCLES, DEFAULT_RESET_CYCLES)
            ),
            CONF_CYCLE_MS: int(item.get(CONF_CYCLE_MS, DEFAULT_CYCLE_MS)),
        }
    return lights


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a version 1 single-light entry to the version 2 lights dict."""
    if entry.version == 1:
        source = str(entry.options.get(CONF_SOURCE) or entry.data.get(CONF_SOURCE))
        lights = {
            _key_for(source, set()): {
                CONF_SOURCE: source,
                CONF_GUARD: float(
                    entry.options.get(CONF_GUARD, entry.data.get(CONF_GUARD, DEFAULT_GUARD))
                ),
                CONF_RESET_CYCLES: int(
                    entry.options.get(
                        CONF_RESET_CYCLES,
                        entry.data.get(CONF_RESET_CYCLES, DEFAULT_RESET_CYCLES),
                    )
                ),
                CONF_CYCLE_MS: int(
                    entry.options.get(
                        CONF_CYCLE_MS, entry.data.get(CONF_CYCLE_MS, DEFAULT_CYCLE_MS)
                    )
                ),
            }
        }
        hass.config_entries.async_update_entry(
            entry, data={CONF_LIGHTS: lights}, options={}, version=2
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up every wrapped light of one entry."""
    store = _get_store(hass)
    data = hass.data[DOMAIN]

    saved = await store.async_load() or {}
    if not data.get("memory"):
        data["memory"] = saved.get("modes", {})

    lights = _entry_lights(entry)
    if not lights:
        _LOGGER.error("TriColor entry %s has no lights configured", entry.entry_id)
        return False

    registry = er.async_get(hass)
    entry_controllers: dict[str, TriColorController] = {}
    for item in lights.values():
        raw_source = item[CONF_SOURCE]
        try:
            source = er.async_validate_entity_id(registry, raw_source)
        except Exception:
            # Fall back to the raw id when the registry cannot resolve it.
            source = raw_source
        memory = data["memory"].get(source, {})
        controller = TriColorController(
            hass=hass,
            entry_id=entry.entry_id,
            source_entity_id=source,
            guard=item[CONF_GUARD],
            reset_cycles=item[CONF_RESET_CYCLES],
            cycle_ms=item[CONF_CYCLE_MS],
            mode=_valid_mode(memory.get("mode", DEFAULT_MODE)),
        )
        entry_controllers[source] = controller
        data["memory"].setdefault(source, {"mode": controller.mode})

        entry.async_on_unload(
            async_track_state_change_event(
                hass, [source], controller.handle_source_state_change
            )
        )

        wrapped = registry.async_get(source)

        def _set_source_entity_id_or_uuid(
            source_entity_id: str, _source: str = source
        ) -> None:
            lights = _entry_lights(entry)
            for key, light in lights.items():
                if light[CONF_SOURCE] == _source:
                    lights[key] = {**light, CONF_SOURCE: source_entity_id}
            hass.config_entries.async_update_entry(
                entry, data={CONF_LIGHTS: lights}
            )
            hass.config_entries.async_schedule_reload(entry.entry_id)

        async def _source_entity_removed(_source: str = source) -> None:
            remaining = {
                key: light
                for key, light in _entry_lights(entry).items()
                if light[CONF_SOURCE] != _source
            }
            if remaining:
                hass.config_entries.async_update_entry(
                    entry, data={CONF_LIGHTS: remaining}
                )
            else:
                await hass.config_entries.async_remove(entry.entry_id)

        entry.async_on_unload(
            async_handle_source_entity_changes(
                hass,
                helper_config_entry_id=entry.entry_id,
                set_source_entity_id_or_uuid=_set_source_entity_id_or_uuid,
                source_device_id=wrapped.device_id if wrapped else None,
                source_entity_id_or_uuid=raw_source,
                source_entity_removed=_source_entity_removed,
            )
        )

    data["controllers"][entry.entry_id] = entry_controllers

    _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload every wrapped light of one entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False
    data = hass.data.get(DOMAIN)
    if data is not None:
        entry_controllers: dict[str, TriColorController] = data["controllers"].pop(
            entry.entry_id, {}
        )
        for controller in entry_controllers.values():
            if controller.tricolor_entity_id is not None:
                data["entity_map"].pop(controller.tricolor_entity_id, None)
        if entry_controllers:
            data["dirty"] = True
            await _async_save(hass)
        if not any(data["controllers"].values()):
            _unregister_services(hass)
    return True


__all__ = [
    "TriColorController",
    "_display_mode",
    "_valid_mode",
]
