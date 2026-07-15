# 🏠 Daikin Comfort Control Smart Temperature

A **Home Assistant custom integration** (HACS-compatible) that adds autonomous, learning-based temperature management on top of [Daikin Comfort Control](https://github.com/Tech-Morph/daikin_comfort_control).

This is a **companion integration** — it does not talk to the Daikin cloud directly. Instead, it attaches to the `DaikinCoordinator` that `daikin_comfort_control` already created, reads the AC's built-in indoor and outdoor temperature sensors (`htemp` / `otemp`), and issues control commands back through the same authenticated API instance. One cloud connection. No duplicated auth. No extra hardware.

> **Part of the Daikin ecosystem:** install [`daikin_comfort_control`](https://github.com/Tech-Morph/daikin_comfort_control) first — it handles the cloud connection, climate entity, and sensors. This repo builds the autonomous decision-making layer on top of it.

---

## How It Works

┌─────────────────────────────────────────────────────────────┐
│ daikin_comfort_control (required)                            │
│                                                                │
│  DaikinCoordinator                    DaikinComfortControlAPI │
│  .data.indoor_temp   ──── read ────►  (already authenticated) │
│  .data.outdoor_temp                   scr.daikincloud.net      │
│  .data.target_temp                                            │
│  .data.mode / fan_rate     │                                  │
│                            │ .set_optimistic_*()   ◄── update │
│                            │ .api.set_device_parameters() ◄── write ──┘ │
└─────────────────────────────────────────────────────────────┘
                           ▲ borrows coordinator at startup
                           │
┌─────────────────────────────────────────────────────────────┐
│ daikin_smart_temperature (this integration)                  │
│                                                                │
│  SmartTemperatureController (async HA background task)       │
│   1. Sleep poll_interval (default 60s)                       │
│   2. Read htemp/otemp °C from coordinator.data               │
│   3. Track outdoor trend (30-min rolling window)              │
│   4. Compute effective target °F (base + time-slot offset)   │
│   5. Compute effective tolerance (tighter if outdoor rising) │
│   6. Check for manual override (app/remote changed state)    │
│   7. Determine mode: cool / heat / fan-only (season-aware)   │
│   8. Determine fan speed, capped at max_fan_mode             │
│   9. Log cycle snapshot to rolling learning log              │
│  10. No-op if AC already at desired mode+fan+setpoint         │
│  11. Short-cycle guard: min 5min between mode switches        │
│  12. Call api.set_device_parameters() + optimistic update    │
└─────────────────────────────────────────────────────────────┘

### Temperature Source

Two sensors, both free — no ESP32, no DHT22, no MQTT broker required:

- **`htemp`** — the thermistor built into the Daikin indoor unit itself
- **`otemp`** — the outdoor temperature reading the unit already reports

Both are read from the coordinator's cached state, not via a separate API poll.

### Mode Selection Logic

| Condition | Mode |
|---|---|
| `htemp` within ±tolerance of target | `fan_only` (just circulate) |
| `htemp` > target + tolerance | `cool` *(if allowed)* |
| `htemp` < target − tolerance | `heat` *(if allowed and season gate passes)* |

**Season-aware heat gate:** in `summer` season mode, heating is only permitted when `htemp` has dropped to/below `summer_heat_min_temp` **and** `otemp` is at/below `outdoor_heat_max` — preventing the heater from firing just because the AC overcooled the house on a mild evening. Optionally restrict heating to nighttime only via `summer_heat_night_only`.

**Pre-cooling:** if outdoor temp has risen more than `precool_rise_threshold` within the last 30 minutes, the effective tolerance band is tightened by `precool_tolerance_cut` — so cooling resumes sooner instead of coasting in fan-only while the afternoon heat climbs.

### Fan Speed Logic

| Delta from target | Fan rate |
|---|---|
| Within tolerance band | `A` (auto) |
| Up to `fan_close_delta` | `2` (low) |
| Up to `fan_mid_delta` | `3` (medium) |
| Beyond `fan_mid_delta` | `4` (high) — capped by `max_fan_mode` |

### Time-of-Day Learning Slots

A fixed offset (°F) is added to the base target temperature during each slot. Defaults:

| Slot | Hours | Default offset |
|---|---|---|
| Morning | 6 am – 9 am | +0 °F |
| Day | 9 am – 5 pm | +1 °F |
| Evening | 5 pm – 10 pm | +1 °F |
| Night | 10 pm – 6 am | −2 °F |

All offsets are editable from the HA options flow — no YAML. Disable entirely to make target temperature exact 24/7.

### Rolling Learning Log

Each control cycle is recorded in memory (outdoor temp, indoor temp, target, mode, timestamp), capped at `learning_log_size` entries. This is currently a data-collection foundation for future empirical tuning — it does not change behavior on its own yet.

### Manual Override Detection

After every command, the controller records what it set (mode, fan, setpoint). On the next poll it compares that to what the coordinator reports as current state. If they differ — meaning someone used the Daikin app or IR remote — automation pauses for `override_timeout` seconds (default 30 min). Set to `0` to disable.

### Why Commands Are Sent as Full Payloads

The Daikin cloud API (`set_control_info`) requires **all fields** on every call — omitting any field causes the unit to revert it to a default. This behaviour was confirmed via mitmproxy capture of the official Android app and is documented in [daikin_comfort_control/daikin_api.py](https://github.com/Tech-Morph/daikin_comfort_control/blob/main/custom_components/daikin_comfort_control/daikin_api.py). Every command from this integration sends the full payload, preserving swing direction and humidity settings from the current coordinator state.

---

## Requirements

- [Daikin Comfort Control](https://github.com/Tech-Morph/daikin_comfort_control) installed, configured, and **successfully polling** in Home Assistant
- Home Assistant 2024.1+

---

## Installation (HACS)

1. Install [Daikin Comfort Control](https://github.com/Tech-Morph/daikin_comfort_control) first and confirm it's polling successfully
2. HACS → Integrations → ⋮ → **Custom Repositories**
3. URL: `https://github.com/Tech-Morph/Daikin-Smart-Temperature` · Category: **Integration**
4. Install **Daikin Comfort Control Smart Temperature** → **Reload/Restart HA**
5. Settings → Devices & Services → **Add Integration** → search `Daikin Smart Temperature`
6. Select your Daikin device (auto-discovered from `daikin_comfort_control`)
7. Configure target temperature, allowed modes, and season settings in the options flow

---

## Entities

| Entity ID | Type | Description |
|---|---|---|
| `switch.daikin_smart_temp_*` | Switch | Enable / disable automation from Lovelace |
| `sensor.daikin_smart_temp_target_*` | Sensor (°F) | Current effective target temp (base + slot offset) |
| `sensor.daikin_smart_temp_mode_*` | Sensor | Last mode the automation commanded |

---

## All Options (Settings → Configure)

| Option | Default | Description |
|---|---|---|
| Target temperature | 72 °F | Base comfort setpoint |
| Tolerance band | ±2 °F | Dead band — no action within this range |
| Min / Max temperature | 65 / 85 °F | Hard clamps on effective target |
| Allow cooling | On | Toggle cool mode |
| Allow heating | On | Toggle heat mode (still gated by season rules below) |
| Allow fan-only | On | Toggle fan-only fallback |
| Maximum fan speed | High | Caps fan rate regardless of delta |
| Season mode | Summer | `normal` or `summer` — controls heat gating behavior |
| Summer heat min temp | 60 °F | Indoor temp must drop to/below this before heat is allowed |
| Summer heat night only | On | Restrict summer heating to the night slot |
| Summer outdoor heat max | 55 °F | Outdoor temp must be at/below this before heat is allowed |
| Pre-cool enabled | On | Tighten tolerance when outdoor temp is rising fast |
| Pre-cool rise threshold | 3 °F | Outdoor rise (over 30 min) that triggers pre-cooling |
| Pre-cool tolerance cut | 0.5 °F | How much to tighten tolerance during pre-cool |
| Learning log enabled | On | Toggle rolling in-memory cycle log |
| Learning log size | 500 | Max entries kept in the rolling log |
| Learning enabled | On | Toggle time-slot offsets |
| Morning / Day / Evening / Night offset | 0 / +1 / +1 / −2 °F | Per-slot adjustments |
| Low fan threshold | 2 °F | Switch to low fan within this delta |
| Medium fan threshold | 4 °F | Switch to medium fan within this delta |
| Poll interval | 60 s | How often to evaluate (min 30 s) |
| Min mode-switch interval | 300 s | Compressor short-cycle guard |
| Override timeout | 1800 s | How long to pause after manual change (0 = off) |

---

## Repo Structure
custom_components/
daikin_smart_temperature/
_init_.py # Entry setup — attaches to daikin_comfort_control coordinator
smart_controller.py # Core async loop: read htemp/otemp → decide → command
config_flow.py # UI setup flow + Options flow
switch.py # Enable/disable switch entity
sensor.py # Target temp + last mode sensor entities
const.py # All constants and defaults
manifest.json # HACS manifest, declares daikin_comfort_control dependency
strings.json # UI label strings
brand/ # Icon/logo for HA frontend (2026.3+)
translations/en.json # English translations
hacs.json
README.md

---

## Related Repos

| Repo | Purpose |
|---|---|
| [daikin_comfort_control](https://github.com/Tech-Morph/daikin_comfort_control) | Required dependency — cloud connection, climate entity, sensors |
| **Daikin-Smart-Temperature** *(this repo)* | Autonomous decision layer — time-of-day learning, season-aware heat gating, outdoor-trend pre-cooling |

---

## License

MIT
