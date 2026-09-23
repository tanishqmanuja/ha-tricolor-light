"""Constants for the TriColor Light integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "tricolor"
STORAGE_KEY: Final = "tricolor.memory"
STORAGE_VERSION: Final = 1

CONF_SOURCE: Final = "source"
CONF_GUARD: Final = "guard_seconds"
CONF_RESET_CYCLES: Final = "reset_cycles"
CONF_CYCLE_MS: Final = "cycle_ms"

#: Domains accepted as a TriColor source.
SOURCE_DOMAINS: Final = ["light"]

DEFAULT_GUARD: Final = 5.2
DEFAULT_RESET_CYCLES: Final = 7
DEFAULT_CYCLE_MS: Final = 300

#: Seconds to wait after a guarded power-on before starting a color procedure,
#: so the driver relay has settled.
SETTLE_SECONDS: Final = 1.0

MODE_COOL: Final = "cool"
MODE_NATURAL: Final = "natural"
MODE_WARM: Final = "warm"

MODES: Final = (MODE_COOL, MODE_NATURAL, MODE_WARM)

#: Hardware cycle order: a fast OFF -> ON step advances one position forward.
CYCLE_ORDER: Final = (MODE_COOL, MODE_NATURAL, MODE_WARM)

DEFAULT_MODE: Final = MODE_WARM

MODE_TO_KELVIN: Final = {
    MODE_WARM: 2700,
    MODE_NATURAL: 4000,
    MODE_COOL: 6500,
}

SERVICE_SET_MODE: Final = "set_mode"
SERVICE_RESET_MODE: Final = "reset_mode"
SERVICE_SET_INTERNAL_STATE: Final = "set_internal_state"

ATTR_MODE: Final = "mode"
#: State attributes exposed on the TriColor light entity.
ATTR_TRICOLOR: Final = "tricolor"
ATTR_PENDING: Final = "tricolor_pending"
