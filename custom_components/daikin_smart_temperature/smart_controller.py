"""
SmartTemperatureController

The core brain. Runs as an async HA background task, reads htemp from
the daikin_comfort_control coordinator (no extra API calls), and issues
set_control_info commands when the temperature drifts outside the
comfort band.

Key design decisions:
  - Uses hass.async_create_background_task() so HA does NOT track this
    coroutine as a setup dependency and the bootstrap watchdog never fires.
  - Sleep is at the END of the loop (not the start), so the very first
    evaluation runs immediately after HA finishes starting up.
  - Always resolves the live ConfigEntry via entry_id so options changes
    take effect on the next poll without a reload.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_TARGET_TEMP, CONF_TOLERANCE, CONF_MIN_TEMP, CONF_MAX_TEMP,
    CONF_POLL_INTERVAL, CONF_MODE_SWITCH_MIN, CONF_OVERRIDE_TIMEOUT,
    CONF_LEARNING_ENABLED,
    CONF_MORNING_OFFSET, CONF_DAY_OFFSET, CONF_EVENING_OFFSET, CONF_NIGHT_OFFSET,
    CONF_FAN_CLOSE_DELTA, CONF_FAN_MID_DELTA,
    DEFAULT_TARGET_TEMP, DEFAULT_TOLERANCE, DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP,
    DEFAULT_POLL_INTERVAL, DEFAULT_MODE_SWITCH_MIN, DEFAULT_OVERRIDE_TIMEOUT,
    DEFAULT_LEARNING_ENABLED,
    DEFAULT_MORNING_OFFSET, DEFAULT_DAY_OFFSET, DEFAULT_EVENING_OFFSET, DEFAULT_NIGHT_OFFSET,
    DEFAULT_FAN_CLOSE_DELTA, DEFAULT_FAN_MID_DELTA,
    FAN_RATE_AUTO, FAN_RATE_LOW, FAN_RATE_MEDIUM, FAN_RATE_HIGH,
    MODE_COOL, MODE_HEAT, MODE_FAN,
)

_LOGGER = logging.getLogger(__name__)

_SLOTS = [
    (dtime(6,  0), dtime(9,  0), CONF_MORNING_OFFSET),
    (dtime(9,  0), dtime(17, 0), CONF_DAY_OFFSET),
    (dtime(17, 0), dtime(22, 0), CONF_EVENING_OFFSET),
    (dtime(22, 0), dtime(6,  0), CONF_NIGHT_OFFSET),
]


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


class SmartTemperatureController:
    """Autonomous temperature control brain."""

    def __init__(self, hass: HomeAssistant, entry_id: str, coordinator) -> None:
        self.hass        = hass
        self._entry_id   = entry_id
        self.coordinator = coordinator
        self._task: asyncio.Task | None = None
        self._enabled: bool = True
        self._last_mode_switch_at: float = 0.0
        self._last_commanded_mode: str | None = None
        self._last_commanded_fan: str | None = None
        self._last_commanded_stemp: float | None = None
        self._override_until: float = 0.0
        self._options_updated_callbacks: list = []

        self.current_target_f: float = DEFAULT_TARGET_TEMP
        self.last_mode: str = "unknown"

    # ------------------------------------------------------------------ live entry

    @property
    def _entry(self) -> ConfigEntry | None:
        """Always return the LIVE entry so options changes are seen immediately."""
        return self.hass.config_entries.async_get_entry(self._entry_id)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the background loop using async_create_background_task.

        Background tasks are NOT tracked by HA's setup/bootstrap watchdog,
        so this will never cause a 'Setup timed out' error regardless of
        how long the loop sleeps.
        """
        self._task = self.hass.async_create_background_task(
            self._loop(), name="daikin_smart_temp_loop"
        )

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        _LOGGER.info("Smart temperature automation %s", "enabled" if enabled else "disabled")

    def register_options_callback(self, cb) -> None:
        self._options_updated_callbacks.append(cb)

    def options_updated(self) -> None:
        """Called by __init__.py update listener when options are saved."""
        self.current_target_f = self._target_temp_f()
        _LOGGER.debug(
            "Options reloaded \u2014 new target=%.1f\u00b0F, max=%.1f\u00b0F",
            self.current_target_f,
            self._opt(CONF_MAX_TEMP, DEFAULT_MAX_TEMP),
        )
        for cb in self._options_updated_callbacks:
            cb()

    # ------------------------------------------------------------------ options helpers

    def _opt(self, key: str, default: Any) -> Any:
        entry = self._entry
        if entry is None:
            return default
        return entry.options.get(key, default)

    def _target_temp_f(self) -> float:
        base   = self._opt(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)
        offset = self._slot_offset()
        raw    = base + offset
        return max(
            self._opt(CONF_MIN_TEMP, DEFAULT_MIN_TEMP),
            min(self._opt(CONF_MAX_TEMP, DEFAULT_MAX_TEMP), raw),
        )

    def _slot_offset(self) -> float:
        if not self._opt(CONF_LEARNING_ENABLED, DEFAULT_LEARNING_ENABLED):
            return 0.0
        now = datetime.now().time()
        for start, end, key in _SLOTS:
            in_slot = (now >= start or now < end) if start > end else (start <= now < end)
            if in_slot:
                return self._opt(key, 0.0)
        return 0.0

    # ------------------------------------------------------------------ logic

    def _determine_mode(self, htemp_f: float, target_f: float) -> str:
        delta = htemp_f - target_f
        tol   = self._opt(CONF_TOLERANCE, DEFAULT_TOLERANCE)
        if abs(delta) <= tol:
            return MODE_FAN
        return MODE_COOL if delta > 0 else MODE_HEAT

    def _determine_fan(self, htemp_f: float, target_f: float) -> str:
        delta = abs(htemp_f - target_f)
        tol   = self._opt(CONF_TOLERANCE, DEFAULT_TOLERANCE)
        if delta <= tol:
            return FAN_RATE_AUTO
        if delta <= self._opt(CONF_FAN_CLOSE_DELTA, DEFAULT_FAN_CLOSE_DELTA):
            return FAN_RATE_LOW
        if delta <= self._opt(CONF_FAN_MID_DELTA, DEFAULT_FAN_MID_DELTA):
            return FAN_RATE_MEDIUM
        return FAN_RATE_HIGH

    def _detect_manual_override(self, current_mode: str, current_fan: str, current_stemp_c: float) -> bool:
        if self._last_commanded_mode is None:
            return False
        current_stemp_f = round(_c_to_f(current_stemp_c))
        last_stemp_f    = round(self._last_commanded_stemp) if self._last_commanded_stemp else None
        return (
            current_mode != self._last_commanded_mode
            or current_fan != self._last_commanded_fan
            or (last_stemp_f is not None and abs(current_stemp_f - last_stemp_f) >= 1)
        )

    # ------------------------------------------------------------------ main loop

    async def _loop(self) -> None:
        """Main control loop. Sleep is at the END so the first cycle runs
        immediately on startup and never blocks the bootstrap phase."""
        _LOGGER.info("SmartTemperatureController started for %s", self.coordinator.device_id)

        # Yield once so HA finishes setup before we do any real work
        await asyncio.sleep(0)

        while True:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                _LOGGER.info("SmartTemperatureController loop cancelled")
                return
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in smart temp loop \u2014 will retry next poll")

            # Sleep AFTER the cycle so boot is never delayed
            poll = self._opt(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            await asyncio.sleep(poll)

    async def _run_cycle(self) -> None:
        """Single evaluation cycle. Called every poll_interval seconds."""
        if not self._enabled:
            return

        if self.coordinator.data is None:
            _LOGGER.debug("Coordinator has no data yet, skipping")
            return

        d = self.coordinator.data
        if not d.power:
            _LOGGER.debug("AC is off \u2014 skipping control cycle")
            return

        htemp_c = d.indoor_temp
        if htemp_c == 0.0:
            _LOGGER.warning("htemp is 0 \u2014 sensor not ready, skipping")
            return

        htemp_f  = _c_to_f(htemp_c)
        target_f = self._target_temp_f()
        self.current_target_f = target_f

        override_timeout = self._opt(CONF_OVERRIDE_TIMEOUT, DEFAULT_OVERRIDE_TIMEOUT)
        if override_timeout > 0 and self._detect_manual_override(
            str(d.mode), d.fan_rate, d.target_temp
        ):
            self._override_until = time.monotonic() + override_timeout
            _LOGGER.info("Manual override detected \u2014 pausing for %ds", override_timeout)

        if time.monotonic() < self._override_until:
            _LOGGER.debug("Override active (%.0fs remaining)", self._override_until - time.monotonic())
            return

        mode = self._determine_mode(htemp_f, target_f)
        fan  = self._determine_fan(htemp_f, target_f)

        target_c  = (target_f - 32) * 5 / 9
        stemp_c   = round(target_c * 2) / 2
        stemp_str = str(stemp_c)

        _LOGGER.info(
            "htemp=%.1f\u00b0F | target=%.1f\u00b0F | delta=%+.1f\u00b0F | mode=%s | fan=%s",
            htemp_f, target_f, htemp_f - target_f, mode, fan,
        )

        current_mode    = str(d.mode)
        current_fan     = d.fan_rate
        current_stemp_c = d.target_temp
        already_ok = (
            current_mode == mode
            and current_fan == fan
            and abs(current_stemp_c - stemp_c) < 0.25
        )
        if already_ok:
            _LOGGER.debug("AC already at desired state \u2014 no command needed")
            self.last_mode = mode
            return

        mode_changed = mode != self._last_commanded_mode
        now = time.monotonic()
        if mode_changed and (now - self._last_mode_switch_at) < self._opt(
            CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN
        ):
            _LOGGER.debug(
                "Mode switch to %s suppressed \u2014 %.0fs since last switch",
                mode, now - self._last_mode_switch_at,
            )
            return

        params: dict[str, Any] = {
            "pow":      "1",
            "mode":     mode,
            "stemp":    stemp_str,
            "dt3":      stemp_str,
            "f_rate":   fan,
            "shum":     "0",
            "f_dir_ud": d.f_dir_ud,
            "f_dir_lr": d.f_dir_lr,
            "dh3":      "0",
        }

        await self.coordinator.api.set_device_parameters(
            self.coordinator.device_id, params
        )
        self.coordinator.set_optimistic_mode(int(mode))
        self.coordinator.set_optimistic_fan_rate(fan)
        self.coordinator.set_optimistic_target_temp(stemp_c)

        self._last_commanded_mode  = mode
        self._last_commanded_fan   = fan
        self._last_commanded_stemp = target_f
        if mode_changed:
            self._last_mode_switch_at = now
        self.last_mode = mode
        _LOGGER.info("Command sent \u2014 mode=%s fan=%s stemp=%.1f\u00b0C", mode, fan, stemp_c)
