"""Smart thermostat controller for Daikin Smart Temperature 1.0.2.

Cooling starts at effective target + cool-on offset, stays on until cool-off,
and exits unconditionally at cold-limit. Idle defaults to fan-only; optional
Off idle can restart when the sensor crosses cool-on. Commands are not proof
of physical state until the coordinator reports them on a later cycle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, time as dtime
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_TARGET_TEMP, CONF_TOLERANCE, CONF_MIN_TEMP, CONF_MAX_TEMP,
    CONF_POLL_INTERVAL, CONF_MODE_SWITCH_MIN, CONF_OVERRIDE_TIMEOUT,
    CONF_LEARNING_ENABLED,
    CONF_MORNING_OFFSET, CONF_DAY_OFFSET, CONF_EVENING_OFFSET, CONF_NIGHT_OFFSET,
    CONF_FAN_CLOSE_DELTA, CONF_FAN_MID_DELTA,
    CONF_ALLOW_COOL, CONF_ALLOW_HEAT, CONF_ALLOW_FAN_ONLY,
    CONF_MAX_FAN_MODE, CONF_SEASON_MODE,
    CONF_SUMMER_HEAT_MIN_TEMP, CONF_SUMMER_HEAT_NIGHT_ONLY,
    CONF_OUTDOOR_HEAT_MAX, CONF_PRECOOL_ENABLED,
    CONF_PRECOOL_RISE_THRESHOLD, CONF_PRECOOL_TOLERANCE_CUT,
    CONF_LEARNING_LOG_ENABLED, CONF_LEARNING_LOG_SIZE,
    CONF_SAFETY_OVERRIDE_DELTA,
    CONF_COOL_ON_DELTA, CONF_COOL_OFF_DELTA, CONF_COLD_LIMIT_DELTA, CONF_IDLE_MODE,
    DEFAULT_COOL_ON_DELTA, DEFAULT_COOL_OFF_DELTA, DEFAULT_COLD_LIMIT_DELTA, DEFAULT_IDLE_MODE,
    IDLE_OFF,
    CONF_FAN_CEILING_ENABLED, CONF_FAN_CEILING_TEMP, CONF_FAN_CEILING_HYSTERESIS,
    CONF_WEATHER_ENTITY, CONF_FORECAST_PRECOOL_ENABLED,
    CONF_FORECAST_HIGH_THRESHOLD, CONF_FORECAST_PRECOOL_TOLERANCE_CUT,
    CONF_FORECAST_CHECK_INTERVAL_CYCLES,
    DEFAULT_TARGET_TEMP, DEFAULT_TOLERANCE, DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP,
    DEFAULT_POLL_INTERVAL, DEFAULT_MODE_SWITCH_MIN, DEFAULT_OVERRIDE_TIMEOUT,
    DEFAULT_LEARNING_ENABLED,
    DEFAULT_MORNING_OFFSET, DEFAULT_DAY_OFFSET, DEFAULT_EVENING_OFFSET, DEFAULT_NIGHT_OFFSET,
    DEFAULT_FAN_CLOSE_DELTA, DEFAULT_FAN_MID_DELTA,
    DEFAULT_ALLOW_COOL, DEFAULT_ALLOW_HEAT, DEFAULT_ALLOW_FAN_ONLY,
    DEFAULT_MAX_FAN_MODE, DEFAULT_SEASON_MODE,
    DEFAULT_SUMMER_HEAT_MIN_TEMP, DEFAULT_SUMMER_HEAT_NIGHT_ONLY,
    DEFAULT_OUTDOOR_HEAT_MAX, DEFAULT_PRECOOL_ENABLED,
    DEFAULT_PRECOOL_RISE_THRESHOLD, DEFAULT_PRECOOL_TOLERANCE_CUT,
    DEFAULT_LEARNING_LOG_ENABLED, DEFAULT_LEARNING_LOG_SIZE,
    DEFAULT_SAFETY_OVERRIDE_DELTA,
    DEFAULT_FAN_CEILING_ENABLED, DEFAULT_FAN_CEILING_TEMP, DEFAULT_FAN_CEILING_HYSTERESIS,
    DEFAULT_FORECAST_PRECOOL_ENABLED, DEFAULT_FORECAST_HIGH_THRESHOLD,
    DEFAULT_FORECAST_PRECOOL_TOLERANCE_CUT, DEFAULT_FORECAST_CHECK_INTERVAL_CYCLES,
    OUTDOOR_TREND_WINDOW_SECONDS,
    FAN_RATE_AUTO, FAN_RATE_LOW, FAN_RATE_MEDIUM, FAN_RATE_HIGH,
    FAN_CAP_AUTO, FAN_CAP_LOW, FAN_CAP_MEDIUM, FAN_CAP_HIGH,
    MODE_COOL, MODE_HEAT, MODE_FAN,
    SEASON_SUMMER,
)

_LOGGER = logging.getLogger(__name__)

_SLOTS = [
    (dtime(6,  0), dtime(9,  0), CONF_MORNING_OFFSET),
    (dtime(9,  0), dtime(17, 0), CONF_DAY_OFFSET),
    (dtime(17, 0), dtime(22, 0), CONF_EVENING_OFFSET),
    (dtime(22, 0), dtime(6,  0), CONF_NIGHT_OFFSET),
]

_STUCK_COMMAND_THRESHOLD = 3
_STUCK_COMMAND_NOTIFICATION_ID = "daikin_smart_temperature_stuck_command"


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

        self.current_target_f: float = self._target_temp_f()
        self.last_mode: str = "unknown"

        self._outdoor_history: deque[tuple[float, float]] = deque()
        self._cycle_log: deque[dict] = deque()

        self._cycle_count: int = 0
        self._forecast_high_f: float | None = None
        self._forecast_fetch_failed: bool = False

        self._cooling_active: bool | None = None
        self._pending_state: tuple[bool, str] | None = None
        self._pending_at: float = 0.0
        self._last_unconfirmed_check: float = 0.0
        self._unconfirmed_count: int = 0
        self._unconfirmed_alerted: bool = False
        self._last_command_at: float = 0.0

    # ------------------------------------------------------------------ live entry

    @property
    def _entry(self) -> ConfigEntry | None:
        return self.hass.config_entries.async_get_entry(self._entry_id)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
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

    def _notify_entities(self) -> None:
        for cb in self._options_updated_callbacks:
            cb()

    def options_updated(self) -> None:
        self.current_target_f = self._target_temp_f()
        self._cooling_active = None
        _LOGGER.debug(
            "Options reloaded — new target=%.1f°F, max=%.1f°F",
            self.current_target_f,
            self._opt(CONF_MAX_TEMP, DEFAULT_MAX_TEMP),
        )
        self._notify_entities()

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

    def _is_night_slot(self) -> bool:
        now = datetime.now().time()
        return now >= dtime(22, 0) or now < dtime(6, 0)

    # ------------------------------------------------------------------ outdoor trend / pre-cooling

    def _record_outdoor_sample(self, outdoor_temp_f: float) -> None:
        now = time.monotonic()
        self._outdoor_history.append((now, outdoor_temp_f))
        cutoff = now - OUTDOOR_TREND_WINDOW_SECONDS
        while self._outdoor_history and self._outdoor_history[0][0] < cutoff:
            self._outdoor_history.popleft()

    def _outdoor_rising_fast(self) -> bool:
        if len(self._outdoor_history) < 2:
            return False
        oldest_temp = self._outdoor_history[0][1]
        newest_temp = self._outdoor_history[-1][1]
        rise = newest_temp - oldest_temp
        threshold = self._opt(CONF_PRECOOL_RISE_THRESHOLD, DEFAULT_PRECOOL_RISE_THRESHOLD)
        return rise >= threshold

    # ------------------------------------------------------------------ Layer 2: forecast

    async def _maybe_refresh_forecast(self) -> None:
        """Fetch today's forecast high every N cycles. Fails safe."""
        if not self._opt(CONF_FORECAST_PRECOOL_ENABLED, DEFAULT_FORECAST_PRECOOL_ENABLED):
            return

        weather_entity = self._opt(CONF_WEATHER_ENTITY, None)
        if not weather_entity:
            return

        interval = int(self._opt(
            CONF_FORECAST_CHECK_INTERVAL_CYCLES, DEFAULT_FORECAST_CHECK_INTERVAL_CYCLES
        ))
        if self._cycle_count % max(1, interval) != 0:
            return

        try:
            response = await asyncio.wait_for(
                self.hass.services.async_call(
                    "weather", "get_forecasts",
                    {"entity_id": weather_entity, "type": "daily"},
                    blocking=True, return_response=True,
                ), timeout=10,
            )
            forecasts = response.get(weather_entity, {}).get("forecast", [])
            if not forecasts:
                _LOGGER.debug("Forecast response empty for %s", weather_entity)
                self._forecast_high_f = None
                return

            today = forecasts[0]
            temp_c = today.get("temperature")
            if temp_c is None:
                self._forecast_high_f = None
                return

            self._forecast_high_f = _c_to_f(float(temp_c))
            self._forecast_fetch_failed = False
            _LOGGER.debug("Forecast high refreshed: %.1f°F", self._forecast_high_f)
        except Exception:  # noqa: BLE001
            if not self._forecast_fetch_failed:
                _LOGGER.warning(
                    "Forecast fetch failed for %s — skipping forecast pre-cool this cycle",
                    weather_entity, exc_info=True,
                )
            self._forecast_fetch_failed = True
            self._forecast_high_f = None

    def _forecast_precool_active(self) -> bool:
        if not self._opt(CONF_FORECAST_PRECOOL_ENABLED, DEFAULT_FORECAST_PRECOOL_ENABLED):
            return False
        if self._forecast_high_f is None:
            return False
        threshold = self._opt(CONF_FORECAST_HIGH_THRESHOLD, DEFAULT_FORECAST_HIGH_THRESHOLD)
        return self._forecast_high_f >= threshold

    def _effective_tolerance(self) -> float:
        tol = self._opt(CONF_TOLERANCE, DEFAULT_TOLERANCE)

        if self._opt(CONF_PRECOOL_ENABLED, DEFAULT_PRECOOL_ENABLED) and self._outdoor_rising_fast():
            cut = self._opt(CONF_PRECOOL_TOLERANCE_CUT, DEFAULT_PRECOOL_TOLERANCE_CUT)
            tol -= cut
            _LOGGER.debug("Outdoor-trend pre-cool active — tolerance cut by %.1f", cut)

        if self._forecast_precool_active():
            cut = self._opt(CONF_FORECAST_PRECOOL_TOLERANCE_CUT, DEFAULT_FORECAST_PRECOOL_TOLERANCE_CUT)
            tol -= cut
            _LOGGER.debug(
                "Forecast pre-cool active (high=%.1f°F) — tolerance cut by %.1f",
                self._forecast_high_f, cut,
            )

        return max(0.25, tol)

    # ------------------------------------------------------------------ mode / fan logic

    def _heat_allowed_now(self, htemp_f: float, outdoor_temp_f: float) -> bool:
        if not self._opt(CONF_ALLOW_HEAT, DEFAULT_ALLOW_HEAT):
            return False

        season_mode = self._opt(CONF_SEASON_MODE, DEFAULT_SEASON_MODE)
        if season_mode != SEASON_SUMMER:
            return True

        summer_min = self._opt(CONF_SUMMER_HEAT_MIN_TEMP, DEFAULT_SUMMER_HEAT_MIN_TEMP)
        if htemp_f > summer_min:
            return False

        outdoor_max = self._opt(CONF_OUTDOOR_HEAT_MAX, DEFAULT_OUTDOOR_HEAT_MAX)
        if outdoor_temp_f > outdoor_max:
            _LOGGER.debug(
                "Summer heat blocked — indoor=%.1f°F is low enough but outdoor=%.1f°F > %.1f°F cap",
                htemp_f, outdoor_temp_f, outdoor_max,
            )
            return False

        if self._opt(CONF_SUMMER_HEAT_NIGHT_ONLY, DEFAULT_SUMMER_HEAT_NIGHT_ONLY):
            return self._is_night_slot()

        return True

    def _cap_fan_rate(self, desired_rate: str) -> str:
        cap = self._opt(CONF_MAX_FAN_MODE, DEFAULT_MAX_FAN_MODE)

        if cap == FAN_CAP_AUTO:
            return FAN_RATE_AUTO
        if cap == FAN_CAP_LOW:
            return FAN_RATE_LOW if desired_rate != FAN_RATE_AUTO else FAN_RATE_AUTO
        if cap == FAN_CAP_MEDIUM:
            if desired_rate == FAN_RATE_HIGH:
                return FAN_RATE_MEDIUM
            return desired_rate
        return desired_rate

    def _thresholds(self, target_f: float) -> tuple[float, float, float]:
        """Cold limit, cool-off, cool-on in Fahrenheit relative to effective target."""
        cold = target_f + float(self._opt(CONF_COLD_LIMIT_DELTA, DEFAULT_COLD_LIMIT_DELTA))
        off = target_f + float(self._opt(CONF_COOL_OFF_DELTA, DEFAULT_COOL_OFF_DELTA))
        on = target_f + float(self._opt(CONF_COOL_ON_DELTA, DEFAULT_COOL_ON_DELTA))
        if not cold < off < on:
            _LOGGER.error("Invalid cooling thresholds; reverting to defaults")
            cold, off, on = target_f - 0.5, target_f + 0.5, target_f + 1.0
        if self._opt(CONF_FAN_CEILING_ENABLED, DEFAULT_FAN_CEILING_ENABLED):
            ceiling = float(self._opt(CONF_FAN_CEILING_TEMP, DEFAULT_FAN_CEILING_TEMP))
            if ceiling > off + 0.25:
                on = min(on, ceiling)
        if self._opt(CONF_PRECOOL_ENABLED, DEFAULT_PRECOOL_ENABLED) and self._outdoor_rising_fast():
            on -= max(0.0, float(self._opt(CONF_PRECOOL_TOLERANCE_CUT, DEFAULT_PRECOOL_TOLERANCE_CUT)))
        if self._forecast_precool_active():
            on -= max(0.0, float(self._opt(CONF_FORECAST_PRECOOL_TOLERANCE_CUT, DEFAULT_FORECAST_PRECOOL_TOLERANCE_CUT)))
        return cold, off, max(off + 0.25, on)

    def _idle_mode(self) -> str:
        if self._opt(CONF_IDLE_MODE, DEFAULT_IDLE_MODE) == IDLE_OFF:
            return IDLE_OFF
        return MODE_FAN if self._opt(CONF_ALLOW_FAN_ONLY, DEFAULT_ALLOW_FAN_ONLY) else IDLE_OFF

    def _choose_mode(self, indoor_f: float, target_f: float, outdoor_f: float,
                     reported_mode: str, powered: bool) -> tuple[str, bool, float, float, float]:
        cold, off, on = self._thresholds(target_f)
        if self._cooling_active is None:
            self._cooling_active = powered and reported_mode == MODE_COOL
        idle = self._idle_mode()
        if indoor_f <= cold:
            self._cooling_active = False
            mode = (MODE_HEAT if self._heat_allowed_now(indoor_f, outdoor_f)
                    and indoor_f < target_f - self._effective_tolerance() else idle)
            return mode, True, cold, off, on
        if self._cooling_active and indoor_f <= off:
            self._cooling_active = False
        elif not self._cooling_active and indoor_f >= on and self._opt(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL):
            self._cooling_active = True
        if self._cooling_active and self._opt(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL):
            return MODE_COOL, False, cold, off, on
        if indoor_f < target_f - self._effective_tolerance() and self._heat_allowed_now(indoor_f, outdoor_f):
            return MODE_HEAT, False, cold, off, on
        return idle, False, cold, off, on

    def _determine_fan(self, htemp_f: float, target_f: float, outdoor_temp_f: float) -> str:
        delta = abs(htemp_f - target_f)
        tol   = self._effective_tolerance()

        if delta <= tol:
            desired = FAN_RATE_AUTO
        elif delta <= self._opt(CONF_FAN_CLOSE_DELTA, DEFAULT_FAN_CLOSE_DELTA):
            desired = FAN_RATE_LOW
        elif delta <= self._opt(CONF_FAN_MID_DELTA, DEFAULT_FAN_MID_DELTA):
            desired = FAN_RATE_MEDIUM
        else:
            desired = FAN_RATE_HIGH

        return self._cap_fan_rate(desired)

    def _detect_manual_override(self, current_mode: str, current_fan: str, current_stemp_c: float) -> bool:
        if self._last_commanded_mode is None:
            return False

        if self._last_commanded_mode == MODE_FAN:
            return (
                current_mode != self._last_commanded_mode
                or current_fan != self._last_commanded_fan
            )

        if current_stemp_c is None:
            return current_mode != self._last_commanded_mode or current_fan != self._last_commanded_fan
        current_stemp_f = round(_c_to_f(current_stemp_c))
        last_stemp_f    = round(self._last_commanded_stemp) if self._last_commanded_stemp else None
        return (
            current_mode != self._last_commanded_mode
            or current_fan != self._last_commanded_fan
            or (last_stemp_f is not None and abs(current_stemp_f - last_stemp_f) >= 1)
        )

    # ------------------------------------------------------------------ stuck-command detection

    def _track_command_confirmation(self, powered: bool, reported_mode: str) -> None:
        """Confirm from later coordinator readback; a mismatch is not proof of hardware failure."""
        if self._pending_state is None or getattr(self.coordinator, "last_update_success", True) is False:
            return
        expected_power, expected_mode = self._pending_state
        if powered == expected_power and (not powered or reported_mode == expected_mode):
            self._pending_state = None
            self._unconfirmed_count = 0
            if self._unconfirmed_alerted:
                persistent_notification.async_dismiss(self.hass, _STUCK_COMMAND_NOTIFICATION_ID)
                _LOGGER.info("Daikin mode/power command confirmed by coordinator")
            self._unconfirmed_alerted = False
            return
        now = time.monotonic()
        if now - self._pending_at < 60 or now - self._last_unconfirmed_check < 60:
            return
        self._last_unconfirmed_check = now
        self._unconfirmed_count += 1
        if self._unconfirmed_count >= _STUCK_COMMAND_THRESHOLD and not self._unconfirmed_alerted:
            message = (
                f"Daikin command power={expected_power}, mode={expected_mode} remains unconfirmed "
                f"after {self._unconfirmed_count} later checks (power={powered}, mode={reported_mode}). "
                "Check cloud readback and the physical unit; the cause is not yet known."
            )
            _LOGGER.error(message)
            persistent_notification.async_create(
                self.hass, message, title="Daikin command unconfirmed",
                notification_id=_STUCK_COMMAND_NOTIFICATION_ID,
            )
            self._unconfirmed_alerted = True

    # ------------------------------------------------------------------ learning log

    def _record_cycle(self, outdoor_f: float, htemp_f: float, target_f: float, mode: str) -> None:
        if not self._opt(CONF_LEARNING_LOG_ENABLED, DEFAULT_LEARNING_LOG_ENABLED):
            return

        max_size = int(self._opt(CONF_LEARNING_LOG_SIZE, DEFAULT_LEARNING_LOG_SIZE))
        self._cycle_log.append({
            "ts": time.time(),
            "outdoor_f": outdoor_f,
            "indoor_f": htemp_f,
            "target_f": target_f,
            "mode": mode,
            "forecast_high_f": self._forecast_high_f,
        })
        while len(self._cycle_log) > max_size:
            self._cycle_log.popleft()

        if len(self._cycle_log) % 50 == 0:
            _LOGGER.debug("Learning log size: %d entries", len(self._cycle_log))

    @property
    def learning_log_size(self) -> int:
        return len(self._cycle_log)

    # ------------------------------------------------------------------ main loop

    async def _loop(self) -> None:
        _LOGGER.info("SmartTemperatureController started for %s", self.coordinator.device_id)

        await asyncio.sleep(0)

        while True:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                _LOGGER.info("SmartTemperatureController loop cancelled")
                return
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in smart temp loop — will retry next poll")

            poll = self._opt(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            await asyncio.sleep(poll)

    async def _run_cycle(self) -> None:
        """One threshold-driven cycle using coordinator data; no extra API reads except refresh."""
        if not self._enabled or getattr(self.coordinator, "last_update_success", True) is False:
            return
        d = self.coordinator.data
        if d is None:
            _LOGGER.debug("Coordinator has no data yet")
            return
        htemp_c = getattr(d, "indoor_temp", None)
        if htemp_c in (None, 0.0):
            _LOGGER.debug("Indoor temperature unavailable; skipping")
            return
        htemp_f = _c_to_f(float(htemp_c))
        outdoor_c = getattr(d, "outdoor_temp", None)
        outdoor_f = _c_to_f(float(outdoor_c)) if outdoor_c not in (None, 0.0) else htemp_f
        self._record_outdoor_sample(outdoor_f)
        self._cycle_count += 1
        await self._maybe_refresh_forecast()
        target_f = self._target_temp_f()
        self.current_target_f = target_f
        powered = bool(d.power)
        reported_mode = str(d.mode)
        self._track_command_confirmation(powered, reported_mode)
        mode, cold_exit, cold, off, on = self._choose_mode(
            htemp_f, target_f, outdoor_f, reported_mode, powered,
        )
        fan = self._determine_fan(htemp_f, target_f, outdoor_f)
        desired_power = mode != IDLE_OFF
        self.last_mode = mode
        self._notify_entities()
        _LOGGER.debug(
            "Indoor %.1f°F target %.1f°F cold/off/on %.1f/%.1f/%.1f; "
            "reported power=%s mode=%s; desired power=%s mode=%s",
            htemp_f, target_f, cold, off, on, powered, reported_mode, desired_power, mode,
        )

        # With fan-only idle, an externally powered-off unit remains off. Off idle
        # deliberately keeps monitoring the sensor so it can restart on heat gain.
        if not powered and self._idle_mode() != IDLE_OFF:
            _LOGGER.debug("AC externally off; leaving it off in fan-only idle configuration")
            return

        now = time.monotonic()
        override_timeout = float(self._opt(CONF_OVERRIDE_TIMEOUT, DEFAULT_OVERRIDE_TIMEOUT))
        if (override_timeout > 0 and powered and self._pending_state is None
                and self._detect_manual_override(reported_mode, str(d.fan_rate), d.target_temp)):
            signature = (reported_mode, str(d.fan_rate), d.target_temp)
            if signature != getattr(self, "_override_signature", None):
                self._override_signature = signature
                self._override_until = now + override_timeout
                _LOGGER.info("Manual override detected; pausing for %.0fs", override_timeout)
        delta = abs(htemp_f - target_f)
        safety_delta = float(self._opt(CONF_SAFETY_OVERRIDE_DELTA, DEFAULT_SAFETY_OVERRIDE_DELTA))
        deescalate = powered and reported_mode == MODE_COOL and mode in (MODE_FAN, IDLE_OFF)
        if now < self._override_until and not (cold_exit or deescalate or delta >= safety_delta):
            _LOGGER.debug("Manual override pause active for %.0fs", self._override_until - now)
            return
        if cold_exit or delta >= safety_delta:
            self._override_until = 0.0

        target_c = (target_f - 32) * 5 / 9
        stemp_c = round(target_c * 2) / 2
        current_stemp = getattr(d, "target_temp", None)
        state_ok = (
            (not desired_power and not powered)
            or (desired_power and powered and reported_mode == mode
                and str(d.fan_rate) == fan
                and (mode == MODE_FAN or (current_stemp is not None
                    and abs(float(current_stemp) - stemp_c) < 0.25)))
        )
        self._record_cycle(outdoor_f, htemp_f, target_f, mode)
        if state_ok:
            return

        desired_state = (desired_power, mode)
        if self._pending_state == desired_state and now - self._last_command_at < 60:
            _LOGGER.debug("Waiting for device readback; no duplicate command")
            return
        if self._pending_state is not None and self._pending_state != desired_state:
            self._pending_state = None
            self._unconfirmed_count = 0
            if self._unconfirmed_alerted:
                persistent_notification.async_dismiss(self.hass, _STUCK_COMMAND_NOTIFICATION_ID)
                self._unconfirmed_alerted = False
        mode_change = (not powered and desired_power) or (powered and
                       (not desired_power or reported_mode != mode))
        if (mode_change and not cold_exit and not deescalate and mode != MODE_COOL
                and delta < safety_delta and now - self._last_mode_switch_at
                < float(self._opt(CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN))):
            _LOGGER.debug("Switch to %s deferred by minimum switch interval", mode)
            return

        command_mode = mode if desired_power else (
            reported_mode if reported_mode in (MODE_COOL, MODE_HEAT, MODE_FAN) else MODE_FAN
        )
        params: dict[str, Any] = {
            "pow": "1" if desired_power else "0",
            "mode": command_mode,
            "stemp": str(stemp_c),
            "dt3": str(stemp_c),
            "f_rate": fan,
            "shum": "0",
            "f_dir_ud": d.f_dir_ud,
            "f_dir_lr": d.f_dir_lr,
            "dh3": "0",
        }
        await self.coordinator.api.set_device_parameters(self.coordinator.device_id, params)
        self._last_command_at = now
        self._pending_state = desired_state
        self._pending_at = now
        self._last_unconfirmed_check = now
        if desired_power:
            self._last_commanded_mode = mode
            self._last_commanded_fan = fan
            self._last_commanded_stemp = target_f
        else:
            self._last_commanded_mode = None
            self._last_commanded_fan = None
            self._last_commanded_stemp = None
        if mode_change:
            self._last_mode_switch_at = now
        _LOGGER.info("Daikin command sent: power=%s mode=%s fan=%s stemp=%.1f°C",
                     desired_power, command_mode, fan, stemp_c)
        try:
            await self.coordinator.async_request_refresh()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Coordinator refresh after command failed; awaiting later readback", exc_info=True)
