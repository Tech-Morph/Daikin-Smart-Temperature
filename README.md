# 🏠 Daikin Comfort Control Smart Temperature

A **Home Assistant custom integration** (HACS-compatible) that adds autonomous,
learning-based temperature management on top of
[Daikin Comfort Control](https://github.com/Tech-Morph/daikin_comfort_control).

This is a **companion integration** — it does not talk to the Daikin cloud
directly. Instead, it attaches to the `DaikinCoordinator` that
`daikin_comfort_control` already created, reads the AC's built-in indoor
temperature sensor (`htemp`), and issues control commands back through the
same authenticated API instance. One cloud connection. No duplicated auth.
No extra hardware.

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│              daikin_comfort_control (required)              │
│                                                             │
│  DaikinCoordinator                 DaikinComfortControlAPI  │
│    .data.indoor_temp  ──── read ──► (already authenticated) │
│    .data.target_temp               scr.daikincloud.net      │
│    .data.mode / fan_rate                    │               │
│    .set_optimistic_*() ◄── update           │               │
│    .api.set_device_parameters() ◄── write ──┘               │
└─────────────────────────────────────────────────────────────┘
              ▲ borrows coordinator at startup
              │
┌─────────────────────────────────────────────────────────────┐
│          daikin_smart_temperature (this integration)        │
│                                                             │
│  SmartTemperatureController  (async HA task, runs forever)  │
│    1. Sleep poll_interval (default 60s)                     │
│    2. Read htemp °C from coordinator.data.indoor_temp       │
│    3. Compute effective target °F (base + time-slot offset) │
│    4. Check for manual override (app/remote changed state)  │
│    5. Determine mode: cool / heat / fan-only                │
│    6. Determine fan speed: auto / low / medium / high       │
│    7. No-op if AC already at desired mode+fan+setpoint      │
│    8. Short-cycle guard: min 5min between mode switches     │
│    9. Call api.set_device_parameters() + optimistic update  │
└─────────────────────────────────────────────────────────────┘
```

### Temperature Source

The only temperature sensor is `htemp` — the thermistor built into the Daikin
indoor unit itself. It is read from the coordinator's cached state, not via a
separate API poll. No ESP32, no DHT22, no MQTT broker required.

### Mode Selection Logic

| Condition | Mode |
|---|---|
| `htemp` within ±tolerance of target | `fan_only` (just circulate) |
| `htemp` > target + tolerance | `cool` |
| `htemp` < target − tolerance | `heat` |

### Fan Speed Logic

| Delta from target | Fan rate |
|---|---|
| Within tolerance band | `A` (auto) |
| Up to `fan_close_delta` | `2` (low) |
| Up to `fan_mid_delta` | `3` (medium) |
| Beyond `fan_mid_delta` | `4` (high) |

### Time-of-Day Learning Slots

A fixed offset (°F) is added to the base target temperature during each slot.
Defaults:

| Slot | Hours | Default offset |
|---|---|---|
| Morning | 6 am – 9 am | +0 °F |
| Day | 9 am – 5 pm | +1 °F |
| Evening | 5 pm – 10 pm | +1 °F |
| Night | 10 pm – 6 am | −2 °F |

All offsets are editable from the HA options flow — no YAML.

### Manual Override Detection

After every command, the controller records what it set (mode, fan, setpoint).
On the next poll it compares that to what the coordinator reports as current
state. If they differ — meaning someone used the Daikin app or IR remote —
automation pauses for `override_timeout` seconds (default 30 min). Set to
`0` to disable.

### Why Commands Are Sent as Full Payloads

The Daikin cloud API (`set_control_info`) requires **all fields** on every
call — omitting any field causes the unit to revert it to a default. This
behaviour was confirmed via mitmproxy capture of the official Android app
and is documented in
[daikin_comfort_control/daikin_api.py](https://github.com/Tech-Morph/daikin_comfort_control/blob/main/custom_components/daikin_comfort_control/daikin_api.py).
Every command from this integration sends the full payload, preserving swing
direction and humidity settings from the current coordinator state.

---

## Requirements

- [Daikin Comfort Control](https://github.com/Tech-Morph/daikin_comfort_control)
  installed, configured, and **successfully polling** in Home Assistant
- Home Assistant 2024.1+

---

## Installation (HACS)

1. HACS → Integrations → ⋮ → **Custom Repositories**
2. URL: `https://github.com/Tech-Morph/smart-learning-house` · Category: **Integration**
3. Install **Daikin Comfort Control Smart Temperature** → **Restart HA**
4. Settings → Devices & Services → **Add Integration** → search `Daikin Smart Temperature`
5. Select your Daikin device (auto-discovered from `daikin_comfort_control`)
6. Configure target temperature and comfort settings in the options flow

---

## Entities

| Entity ID | Type | Description |
|---|---|---|
| `switch.daikin_smart_temp_<id>` | Switch | Enable / disable automation from Lovelace |
| `sensor.daikin_smart_temp_target_<id>` | Sensor (°F) | Current effective target temp (base + slot offset) |
| `sensor.daikin_smart_temp_mode_<id>` | Sensor | Last mode the automation commanded |

---

## All Options (Settings → Configure)

| Option | Default | Description |
|---|---|---|
| Target temperature | 72 °F | Base comfort setpoint |
| Tolerance band | ±2 °F | Dead band — no action within this range |
| Min / Max temperature | 65 / 85 °F | Hard clamps on effective target |
| Learning enabled | On | Toggle time-slot offsets |
| Morning / Day / Evening / Night offset | 0 / +1 / +1 / −2 °F | Per-slot adjustments |
| Low fan threshold | 2 °F | Switch to low fan within this delta |
| Medium fan threshold | 4 °F | Switch to medium fan within this delta |
| Poll interval | 60 s | How often to evaluate (min 30 s) |
| Min mode-switch interval | 300 s | Compressor short-cycle guard |
| Override timeout | 1800 s | How long to pause after manual change (0 = off) |

---

## Repo Structure

```
custom_components/
  daikin_smart_temperature/
    __init__.py          # Entry setup — attaches to daikin_comfort_control coordinator
    smart_controller.py  # Core async loop: read htemp → decide → command
    config_flow.py       # UI setup flow + Options flow
    switch.py            # Enable/disable switch entity
    sensor.py            # Target temp + last mode sensor entities
    const.py             # All constants and defaults
    manifest.json        # HACS manifest, declares daikin_comfort_control dependency
    strings.json         # UI label strings
    translations/en.json # English translations
hacs.json
README.md
```

---

## License

MIT
