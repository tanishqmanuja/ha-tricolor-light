# TriColor Light

A Home Assistant integration for Philips UltraGlow tunable-white (tri-color) drivers, like the [Philips UltraGlow downlight](https://in.shop.lighting.philips.com/products/philips-ultraglow-downlight-tunable-white?variant=43087040315582).

These drivers change color on fast off/on power cycles, so quick toggles can silently change your light from warm to cool without you asking. TriColor Light remembers the real hardware color, protects normal on/off use from cycling it, and exposes the light as a proper color-temperature light that works with scenes, dashboards, and voice assistants.

## Requirements

- An existing `light` entity driving the tri-color fixture (for example, a wall switch exposed as a light).
- Home Assistant 2026.9 or newer.

## Supported hardware

Built for the Philips UltraGlow tunable-white downlight:

**Product:** [Philips UltraGlow downlight tunable white](https://in.shop.lighting.philips.com/products/philips-ultraglow-downlight-tunable-white?variant=43087040315582)

**Driver datasheet specs:**

| Spec | Value |
|---|---|
| Color cycle | COOL → NATURAL → WARM → COOL |
| Cool Daylight | 6500 K |
| Cool White (Natural) | 4000 K |
| Warm White | 2700 K |
| OFF → ON within 2 s | Advances to the next color |
| OFF → ON after 5 s | Retains the last color |
| 7 quick ON/OFF toggles | Resets to Cool Daylight (light must be ON first) |

Other tri-color drivers with the same step-on-fast-toggle behavior should work too — adjust the guard and cycle timing in the entry options to match.

## Installation

### Via HACS (recommended)

1. Open HACS, then Integrations.
2. Open the menu and choose **Custom repositories**.
3. Add your repository URL with category **Integration**.
4. Find **TriColor Light** and install it.
5. Restart Home Assistant.

### Manual

Copy `custom_components/tricolor` into your Home Assistant `custom_components` folder and restart.

## Setup

1. Go to **Settings → Devices & services → Add integration → TriColor Light**.
2. Pick the existing light. A new `<name> TriColor` light is created on the same device; the original light keeps working.
3. Fine-tune the timing under the entry's **Configure** button if needed (saving reloads automatically):

| Option | Default | What it does |
|---|---|---|
| Guard (seconds) | 5.2 | Minimum gap before an ON after any change, so normal use never steps the color |
| Reset cycles | 7 | Power toggles used by the hardware reset procedure |
| Cycle step (ms) | 300 | ON/OFF step length for color procedures |

## What you get

- A `<name> TriColor` light with real color temperature: Warm 2700 K, Natural 4000 K, Cool 6500 K. Turning it on with a kelvin value steps the hardware to the nearest color.
- Full scene support: scenes store and replay the color temperature.
- The light reports 100% brightness while on (the driver has no dimming; the slider simply turns it on).
- State attributes for dashboards: `tricolor` (`Warm`/`Natural`/`Cool`) and `tricolor_pending` (`true` while an ON is waiting out the guard window — handy for button animations).

## Services

All services target the TriColor light entities.

```yaml
# Step the hardware to a color (skips if already there)
service: tricolor.set_mode
data:
  mode: natural
target:
  entity_id: light.ceiling_tricolor

# Hardware reset back to cool daylight (runs all 7 toggles)
service: tricolor.reset_mode
target:
  entity_id: light.ceiling_tricolor

# Correct the remembered color WITHOUT touching the light.
# Use after something outside Home Assistant flipped the light quickly.
service: tricolor.set_internal_state
data:
  mode: warm
target:
  entity_id: light.ceiling_tricolor
```

## How it works

Per the driver design, a fast OFF → ON (within ~2 s) advances the color cool → natural → warm → cool, while staying OFF longer than ~5 s keeps it. Seven quick toggles reset to cool daylight. The integration spaces every power step through the guard window and runs the stepping procedure whenever a different color is requested, all serialized so stacked calls and scenes never race the relay.

## Troubleshooting

- **Color looks wrong after someone used the wall switch?** The hardware was flipped outside Home Assistant. Either run `tricolor.reset_mode` to re-baseline to cool, or fix just the memory with `tricolor.set_internal_state`.
- **Memory is kept** across restarts. After a reboot the first ON waits out one safe guard window.
