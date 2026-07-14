# 🏠 Smart Learning House

An AI-driven temperature management system that monitors your home's temperature via MQTT sensors and autonomously controls your **Daikin AC/Heater** — adjusting mode, fan speed, and setpoint to maintain comfort while learning your preferences over time.

## Features

- 📡 Real-time temperature monitoring via MQTT (ESP32/ESPHome sensors)
- 🧠 Learning controller that adapts setpoint targets based on time-of-day and history
- 🌀 Daikin LAN API integration — controls mode (cool/heat/fan/auto), fan speed, and target temp
- 📊 SQLite logging for historical analysis and model training
- ⚙️ YAML-based configuration (no hardcoded secrets)
- 🐳 Docker Compose ready

## Architecture

```
[ESP32 Sensors] → MQTT Broker → [smart-brain.py] → Daikin LAN API
                                       ↓
                               SQLite DB (logs)
```

## Quick Start

### 1. Configure
```bash
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml with your MQTT broker, Daikin IP, and comfort settings
```

### 2. Run with Docker
```bash
docker compose up -d
```

### 3. Run Directly (Python)
```bash
pip install -r requirements.txt
python src/smart_brain.py
```

## Sensor Setup (ESPHome)

See `esphome/temperature_sensor.yaml` for a ready-to-flash ESP32 config that publishes temperature to MQTT.

## Config Reference

See `config/config.example.yaml` for all options.

## License

MIT
