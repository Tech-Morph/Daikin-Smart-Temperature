# 🏠 Smart Learning House

An autonomous temperature management system that reads your home's temperature
directly from the **Daikin AC's built-in indoor sensor** (`htemp`) and
controls the unit via the **Daikin Comfort Control cloud API** — the same
cloud stack reverse-engineered in [daikin_comfort_control](https://github.com/Tech-Morph/daikin_comfort_control).

No extra sensors, no MQTT broker, no ESPHome required.

## How It Works

```
[Daikin Cloud API]
        │
        ├── GET /aircon/get_sensor_info  →  htemp (indoor °C from AC sensor)
        ├── GET /aircon/get_control_info  →  current mode, fan, setpoint
        └── GET /aircon/set_control_info  ←  SmartBrain pushes new params
                  ↑
          [SmartBrain]
            - Reads htemp every 60s
            - Runs LearningEngine (time-slot target + PID correction)
            - Determines mode (cool/heat/fan) and fan speed
            - Rate-limits commands to avoid API hammering
            - Logs everything to SQLite
```

## Key Design Decisions

- **`htemp` as the sole sensor** — the AC unit's indoor thermistor. Not room-center accurate, but good enough for setpoint-based control. Works with zero extra hardware.
- **Cloud API reuse** — uses the same auth + endpoint logic from `daikin_comfort_control/daikin_api.py`. No duplication; the `daikin_api.py` file is symlinked/copied in.
- **No Home Assistant dependency** — runs standalone as a Docker container or Python process.
- **20s command cooldown** — mirrors coordinator logic; skips polling immediately after issuing a command to avoid reading stale cloud state.

## Quick Start

```bash
git clone https://github.com/Tech-Morph/smart-learning-house
cd smart-learning-house
cp config/config.example.yaml config/config.yaml
# Fill in your Daikin username, password, uid
nano config/config.yaml

docker compose -f docker/docker-compose.yml up -d
```

## Config

See `config/config.example.yaml`. The `daikin.uid` value is the `x-daikin-uid`
header — a static device fingerprint string captured from the app.

## Logs

```bash
docker logs -f smart-learning-house
# or view SQLite:
sqlite3 data/sensor_log.db 'SELECT * FROM temperature_log ORDER BY ts DESC LIMIT 20;'
```

## License

MIT
