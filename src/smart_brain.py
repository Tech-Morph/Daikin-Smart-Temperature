#!/usr/bin/env python3
"""
Smart Learning House - Main Control Brain
Monitors temperature via MQTT and controls Daikin AC/heater.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

import yaml
import paho.mqtt.client as mqtt

from daikin_controller import DaikinController
from learning_engine import LearningEngine
from sensor_db import SensorDB

# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# SmartBrain
# ---------------------------------------------------------------------------

class SmartBrain:
    def __init__(self, config: dict):
        self.cfg = config
        self.logger = logging.getLogger("SmartBrain")
        self.current_temps: dict[str, float] = {}  # room -> temp
        self.last_mode_switch = 0.0

        self.daikin = DaikinController(
            ip=config["daikin"]["ip"],
            port=config["daikin"].get("port", 80),
        )
        self.db = SensorDB(config["logging"]["db_path"])
        self.learning = LearningEngine(config, self.db)

        # MQTT setup
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.username_pw_set(
            config["mqtt"]["username"],
            config["mqtt"]["password"],
        )
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            topic = f"{self.cfg['mqtt']['topic_prefix']}/#"
            client.subscribe(topic)
            self.logger.info(f"MQTT connected. Subscribed to {topic}")
        else:
            self.logger.error(f"MQTT connection failed: rc={rc}")

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            # Expected topic: home/sensors/<room>/temperature
            parts = msg.topic.split("/")
            if len(parts) >= 3 and parts[-1] == "temperature":
                room = parts[-2]
                temp = float(msg.payload.decode())
                use_celsius = self.cfg["comfort"].get("use_celsius", False)
                if use_celsius:
                    temp = (temp * 9 / 5) + 32  # Normalize to Fahrenheit internally
                self.current_temps[room] = temp
                self.db.log_temperature(room, temp)
                self.logger.debug(f"Sensor [{room}]: {temp:.1f}°F")
        except Exception as e:
            self.logger.error(f"Error parsing MQTT message: {e}")

    def _average_temp(self) -> float | None:
        if not self.current_temps:
            return None
        return sum(self.current_temps.values()) / len(self.current_temps)

    def _determine_fan_speed(self, delta: float) -> str:
        fan_cfg = self.cfg["fan_speed"]
        abs_delta = abs(delta)
        if abs_delta <= fan_cfg["low_delta"]:
            return "1"   # Auto/quiet
        elif abs_delta <= fan_cfg["medium_delta"]:
            return "3"   # Medium
        else:
            return "4"   # High

    def _determine_mode(self, avg_temp: float, target: float) -> str:
        """Returns Daikin mode: cool, heat, fan, auto"""
        delta = avg_temp - target
        tolerance = self.cfg["comfort"]["tolerance"]
        if abs(delta) <= tolerance:
            return "fan"   # Within tolerance — just circulate
        elif delta > 0:
            return "cool"  # Too hot
        else:
            return "heat"  # Too cold

    async def control_loop(self):
        poll = self.cfg["control"]["poll_interval"]
        min_switch = self.cfg["control"]["min_mode_switch_interval"]

        while True:
            await asyncio.sleep(poll)
            avg = self._average_temp()
            if avg is None:
                self.logger.warning("No sensor data yet — skipping control cycle")
                continue

            target = self.learning.get_current_target()
            delta = avg - target
            mode = self._determine_mode(avg, target)
            fan = self._determine_fan_speed(delta)

            self.logger.info(
                f"Avg temp: {avg:.1f}°F | Target: {target:.1f}°F | "
                f"Delta: {delta:+.1f}°F | Mode: {mode} | Fan: {fan}"
            )

            # Rate-limit mode switches to prevent rapid cycling
            now = time.time()
            if (now - self.last_mode_switch) >= min_switch:
                try:
                    await self.daikin.set_control(mode=mode, fan=fan, setpoint=target)
                    self.last_mode_switch = now
                    self.db.log_control_action(avg, target, mode, fan)
                except Exception as e:
                    self.logger.error(f"Daikin control error: {e}")
            else:
                self.logger.debug("Mode switch rate-limited — skipping")

    def run(self):
        logging.basicConfig(
            level=self.cfg["logging"].get("level", "INFO"),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
        self.mqtt_client.connect(
            self.cfg["mqtt"]["broker"],
            self.cfg["mqtt"]["port"],
        )
        self.mqtt_client.loop_start()
        self.logger.info("Smart Learning House brain started")
        asyncio.run(self.control_loop())


if __name__ == "__main__":
    cfg = load_config()
    brain = SmartBrain(cfg)
    brain.run()
