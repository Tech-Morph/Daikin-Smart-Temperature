#!/usr/bin/env python3
"""
Smart Learning House — Main Control Brain

Uses the Daikin Comfort Control cloud API (same stack as daikin_comfort_control
custom component) to read htemp and issue control commands.

No MQTT. No ESPHome. No local network access required.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import yaml

from daikin_client import DaikinClient
from learning_engine import LearningEngine
from sensor_db import SensorDB

_LOGGER = logging.getLogger("SmartBrain")


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class SmartBrain:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = SensorDB(cfg["logging"]["db_path"])
        self.learning = LearningEngine(cfg)
        self._last_command_at: float = 0.0
        self._last_mode_switch_at: float = 0.0
        self._last_mode: str | None = None

    def _determine_mode(self, htemp: float, target: float) -> str:
        """cool | heat | fan based on delta vs tolerance band."""
        delta = htemp - target
        tol = self.cfg["comfort"]["tolerance_c"]
        if abs(delta) <= tol:
            return "fan"
        return "cool" if delta > 0 else "heat"

    def _determine_fan(self, htemp: float, target: float) -> str:
        fc = self.cfg["fan_speed"]
        delta = abs(htemp - target)
        tol = self.cfg["comfort"]["tolerance_c"]
        if delta <= tol:
            return fc["within_tolerance"]
        if delta <= fc["close_range_c"]:
            return fc["close_fan"]
        if delta <= fc["mid_range_c"]:
            return fc["mid_fan"]
        return fc["far_fan"]

    # Daikin mode string -> API mode int
    MODE_MAP = {"cool": "3", "heat": "4", "fan": "6", "auto": "1", "dry": "2"}

    async def control_loop(self, client: DaikinClient):
        poll = self.cfg["control"]["poll_interval_s"]
        cooldown = self.cfg["control"]["command_cooldown_s"]
        mode_switch_min = self.cfg["control"]["min_mode_switch_interval_s"]

        while True:
            await asyncio.sleep(poll)

            # Skip read if we just sent a command (cloud propagation lag)
            since_cmd = time.monotonic() - self._last_command_at
            if self._last_command_at and since_cmd < cooldown:
                _LOGGER.debug("Within command cooldown (%.1fs left) — skipping poll", cooldown - since_cmd)
                continue

            try:
                state = await client.get_state()
            except Exception as e:
                _LOGGER.error("Failed to read device state: %s", e)
                continue

            htemp = state["htemp"]
            target = self.learning.get_current_target()
            mode_str = self._determine_mode(htemp, target)
            fan = self._determine_fan(htemp, target)
            mode_code = self.MODE_MAP[mode_str]

            _LOGGER.info(
                "htemp=%.1f°C | target=%.1f°C | delta=%+.1f | mode=%s | fan=%s",
                htemp, target, htemp - target, mode_str, fan,
            )

            self.db.log_temperature(htemp, target, state.get("otemp", 0.0))

            # Rate-limit mode switches (protect compressor)
            now = time.monotonic()
            mode_changed = mode_str != self._last_mode
            time_since_switch = now - self._last_mode_switch_at

            if mode_changed and time_since_switch < mode_switch_min:
                _LOGGER.debug(
                    "Mode switch to '%s' suppressed — %.0fs since last switch (min %ds)",
                    mode_str, time_since_switch, mode_switch_min,
                )
                continue

            # Always skip command if already in desired state within tolerance
            current_mode = str(state.get("mode", ""))
            current_fan = str(state.get("f_rate", ""))
            current_stemp = float(state.get("stemp", 0.0))
            desired_stemp = round(target, 1)

            already_correct = (
                current_mode == mode_code
                and current_fan == fan
                and abs(current_stemp - desired_stemp) < 0.1
            )
            if already_correct:
                _LOGGER.debug("AC already at desired state — no command needed")
                continue

            try:
                await client.set_control(
                    mode=mode_code,
                    fan=fan,
                    stemp=desired_stemp,
                )
                self._last_command_at = time.monotonic()
                if mode_changed:
                    self._last_mode_switch_at = now
                    self._last_mode = mode_str
                self.db.log_control_action(htemp, target, mode_str, fan)
                _LOGGER.info("Command sent OK")
            except Exception as e:
                _LOGGER.error("Failed to set control: %s", e)

    async def run(self):
        logging.basicConfig(
            level=self.cfg["logging"].get("level", "INFO"),
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
        async with aiohttp.ClientSession() as session:
            client = DaikinClient(
                username=self.cfg["daikin"]["username"],
                password=self.cfg["daikin"]["password"],
                uid=self.cfg["daikin"]["uid"],
                session=session,
            )
            await client.connect()
            _LOGGER.info("Smart Learning House started. Device: %s", client.device_id)
            await self.control_loop(client)


if __name__ == "__main__":
    cfg = load_config()
    brain = SmartBrain(cfg)
    asyncio.run(brain.run())
