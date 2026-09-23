"""Color-temperature light entity wrapping an existing tri-color light.

The entity is added to the same device as the wrapped light by linking the
source device entry. The wrapped light is left visible and untouched, so
existing automations keep working next to this one.

Naming is deterministic: entity id ``<source_object_id>_tricolor`` and name
``<source name> TriColor``.

Scenes work through native color temperature: the entity is ``COLOR_TEMP``
only (2700/4000/6500 K), so scenes capture ``color_temp_kelvin`` and replay it
through :meth:`TriColorLight.async_turn_on`, which awaits the guarded
power-step procedure.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import TriColorController, _display_mode
from .const import (
    ATTR_PENDING,
    ATTR_TRICOLOR,
    DOMAIN,
    MODE_TO_KELVIN,
    MODES,
)

_LOGGER = logging.getLogger(__name__)


def _nearest_mode(kelvin: int) -> str:
    """Map any kelvin value to the closest hardware mode."""
    return min(MODES, key=lambda mode: abs(MODE_TO_KELVIN[mode] - kelvin))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the TriColor light for one wrapped source entity."""
    controller = hass.data.get(DOMAIN, {}).get("controllers", {}).get(entry.entry_id)
    if controller is None:
        _LOGGER.error("TriColor controller missing for entry %s", entry.entry_id)
        return
    async_add_entities([TriColorLight(hass, entry, controller)])


class TriColorLight(LightEntity):
    """Color-temperature mirror of an existing tri-color light."""

    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 6500
    _attr_should_poll = False
    # Correct as "none": the driver supports on/off + color temperature only
    # (no effects, flash or transition). Color-temp capability is expressed
    # through supported_color_modes, not features.
    _attr_supported_features = LightEntityFeature(0)

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller: TriColorController
    ) -> None:
        """Link to the source device as ``<name> TriColor`` / ``<id>_tricolor``."""
        self._controller = controller
        self._source = controller.source_entity_id

        registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
        wrapped = registry.async_get(self._source)

        base_name: str = entry.title
        if wrapped is not None:
            base_name = (
                wrapped.name
                or wrapped.original_name
                or (state.name if (state := hass.states.get(self._source)) else None)
                or entry.title
            )
            self._attr_entity_category = wrapped.entity_category
            self._attr_icon = wrapped.icon or wrapped.original_icon
            if wrapped.device_id and (
                device := device_registry.async_get(wrapped.device_id)
            ):
                # Share the source device so both entities appear together.
                self.device_entry = device

        # Fixed name and suggested object id: the entity id is always
        # <source_object_id>_tricolor.
        self._attr_name = f"{base_name} TriColor"
        self._attr_unique_id = entry.entry_id
        self._attr_suggested_object_id = f"{self._source.split('.', 1)[1]}_tricolor"

        self._is_on = False
        self._available = True

    @callback
    def _refresh(self) -> None:
        # A missing or unavailable source reads unavailable; "unknown"
        # (never toggled) reads off.
        state = self.hass.states.get(self._source)
        if state is None or state.state == STATE_UNAVAILABLE:
            self._available = False
        else:
            self._available = True
            self._is_on = state.state == STATE_ON
        self.async_write_ha_state()

    @callback
    def _source_listener(self, event: Event[EventStateChangedData] | None = None) -> None:
        self._refresh()

    async def async_added_to_hass(self) -> None:
        """Follow the source and register for controller updates."""
        self.async_on_remove(self._controller.subscribe_refresh(self._refresh))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source], self._source_listener
            )
        )
        self._controller.tricolor_entity_id = self.entity_id
        self.hass.data[DOMAIN]["entity_map"][self.entity_id] = self._controller.entry_id
        self._refresh()

    @property
    def available(self) -> bool:
        """Return True when the wrapped source state is known."""
        return self._available

    @property
    def is_on(self) -> bool:
        """Return True when the wrapped source is on."""
        return self._is_on

    @property
    def color_mode(self) -> ColorMode | None:
        """Return COLOR_TEMP while on (scenes key off this)."""
        if not self._is_on:
            return None
        return ColorMode.COLOR_TEMP

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the remembered mode as kelvin while on."""
        if not self._is_on:
            return None
        return MODE_TO_KELVIN[self._controller.mode]

    @property
    def brightness(self) -> int | None:
        """Report full brightness while on.

        The relay driver has no dimming, and HA always shows a brightness
        slider for COLOR_TEMP lights, so report 100% (255) while on and no
        brightness while off. Incoming brightness values are accepted and
        ignored, except 0 which turns the light off.
        """
        if not self._is_on:
            return None
        return 255

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the remembered mode and guard state for dashboards."""
        return {
            ATTR_TRICOLOR: _display_mode(self._controller.mode),
            ATTR_PENDING: self._controller.pending,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally stepping the hardware to a requested kelvin.

        A scene replay (or any ``light.turn_on`` carrying color temperature)
        maps to the nearest hardware mode and runs the power-step procedure.
        Everything is awaited under the controller lock so scenes and stacked
        calls serialize instead of racing the driver relay.
        """
        kelvin: int | None = None
        if (
            ATTR_COLOR_TEMP_KELVIN in kwargs
            and kwargs[ATTR_COLOR_TEMP_KELVIN] is not None
        ):
            kelvin = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None and int(brightness) == 0:
            await self._controller.async_turn_off()
            self._refresh()
            return
        if kelvin is not None:
            await self._controller.async_set_mode(_nearest_mode(kelvin))
        else:
            await self._controller.async_turn_on_guarded()
        self._refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off. Cutting power never cycles the driver color."""
        await self._controller.async_turn_off()
        self._refresh()
