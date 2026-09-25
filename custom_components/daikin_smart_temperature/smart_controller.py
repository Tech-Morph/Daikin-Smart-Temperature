"""Smart Daikin thermostat with Cool-on, Cool-off, cold-limit and Fan Only idle.

The cold-limit takes precedence over forecast and ordinary mode timing.
Reported state is not assumed to prove physical command execution.
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
    IDLE_FAN_ONLY, IDLE_OFF,
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

        # Stuck-command detection
        self._consecutive_ceiling_failures: int = 0
        self._last_ceiling_command_mode: str | None = None
        self._stuck_command_alerted: bool = False

        # Ceiling hysteresis latch
        self._cooling_active: bool | None = None
        self._pending_mode: str | None = None
        self._pending_at: float = 0.0
        self._last_command_at: float = 0.0
        self._unconfirmed_cycles: int = 0
        self._state_unconfirmed_alerted: bool = False

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
            response = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": weather_entity, "type": "daily"},
                blocking=True, return_response=True,
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

            self._forecast_high_f = _c_to_f(temp_c)
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
        """Cold limit, cooling stop, and cooling start in Fahrenheit.

        Trend and forecast may advance the start but never lower the stop
        or cold-limit. The legacy ceiling can only lower the start if it
        is ABOVE the stop; an old 68F ceiling no longer cools to 66F.
        """
        cold = target_f + float(self._opt(CONF_COLD_LIMIT_DELTA, DEFAULT_COLD_LIMIT_DELTA))
        off = target_f + float(self._opt(CONF_COOL_OFF_DELTA, DEFAULT_COOL_OFF_DELTA))
        on = target_f + float(self._opt(CONF_COOL_ON_DELTA, DEFAULT_COOL_ON_DELTA))
        if not cold < off < on:
            _LOGGER.error("Invalid cooling thresholds; using safe defaults")
            cold, off, on = target_f - 0.5, target_f + 0.5, target_f + 1.0
        if self._opt(CONF_FAN_CEILING_ENABLED, DEFAULT_FAN_CEILING_ENABLED):
            ceiling = float(self._opt(CONF_FAN_CEILING_TEMP, DEFAULT_FAN_CEILING_TEMP))
            if ceiling >= off + 0.25:
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

    def _choose_mode(
        self, indoor_f: float, target_f: float, outdoor_f: float, reported: str,
    ) -> tuple[str, bool, float, float, float]:
        """Stop cooling on cold-limit BEFORE any other decision."""
        cold, off, on = self._thresholds(target_f)
        if self._cooling_active is None:
            self._cooling_active = reported == MODE_COOL
        idle = self._idle_mode()
        if indoor_f <= cold:
            was_cooling = self._cooling_active or reported == MODE_COOL
            self._cooling_active = False
            if not was_cooling and indoor_f < target_f - self._effective_tolerance() and self._heat_allowed_now(indoor_f, outdoor_f):
                return MODE_HEAT, False, cold, off, on
            return idle, True, cold, off, on
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

        current_stemp_f = round(_c_to_f(current_stemp_c))
        last_stemp_f    = round(self._last_commanded_stemp) if self._last_commanded_stemp else None
        return (
            current_mode != self._last_commanded_mode
            or current_fan != self._last_commanded_fan
            or (last_stemp_f is not None and abs(current_stemp_f - last_stemp_f) >= 1)
        )

    # ------------------------------------------------------------------ stuck-command detection

    def _track_command_confirmation(self, reported: str) -> None:
        """Treat nonmatching readback as uncertain, not a proven hardware fault."""
        if self._pending_mode is None:
            return
        if reported == self._pending_mode:
            self._pending_mode = None
            self._unconfirmed_cycles = 0
            if self._state_unconfirmed_alerted:
                persistent_notification.async_dismiss(self.hass, _STUCK_COMMAND_NOTIFICATION_ID)
                _LOGGER.info("Daikin mode change now matches reported state")
            self._state_unconfirmed_alerted = False
            return
        if time.monotonic() - self._pending_at < 90 or not getattr(self.coordinator, "last_update_success", True):
            return
        self._unconfirmed_cycles += 1
        if self._unconfirmed_cycles >= 3 and not self._state_unconfirmed_alerted:
            msg = (
                f"Daikin mode {self._pending_mode} remains unconfirmed after multiple polls "
                f"(reported {reported}). Check the unit and cloud connection."
            )
            _LOGGER.error(msg)
            persistent_notification.async_create(
                self.hass, msg, title="Daikin mode unconfirmed",
                notification_id=_STUCK_COMMAND_NOTIFICATION_ID,
            )
            self._state_unconfirmed_alerted = True

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
        """Single evaluation cycle. Called every poll_interval seconds."""
        if not self._enabled:
            return

        if self.coordinator.data is None:
            _LOGGER.debug("Coordinator has no data yet, skipping")
            return

        d = self.coordinator.data
        if not d.power:
            _LOGGER.debug("AC is off — skipping control cycle")
            return

        htemp_c = d.indoor_temp
        if htemp_c == 0.0:
            _LOGGER.warning("htemp is 0 — sensor not ready, skipping")
            return

        htemp_f = _c_to_f(htemp_c)

        outdoor_c = getattr(d, "outdoor_temp", None)
        outdoor_f = _c_to_f(outdoor_c) if outdoor_c not in (None, 0.0) else htemp_f
        if outdoor_c in (None, 0.0):
            _LOGGER.debug("otemp unavailable this cycle — using htemp as fallback")

        self._record_outdoor_sample(outdoor_f)
        self._cycle_count += 1
        await self._maybe_refresh_forecast()

        target_f = self._target_temp_f()
        self.current_target_f = target_f

        _LOGGER.debug(
            "season=%s allow_cool=%s allow_heat=%s max_fan=%s outdoor=%.1f°F precool_active=%s forecast_high=%s ceiling=%.1f°F",
            self._opt(CONF_SEASON_MODE, DEFAULT_SEASON_MODE),
            self._opt(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL),
            self._opt(CONF_ALLOW_HEAT, DEFAULT_ALLOW_HEAT),
            self._opt(CONF_MAX_FAN_MODE, DEFAULT_MAX_FAN_MODE),
            outdoor_f,
            self._outdoor_rising_fast(),
            self._forecast_high_f,
            self._opt(CONF_FAN_CEILING_TEMP, DEFAULT_FAN_CEILING_TEMP),
        )

        current_mode_reported = str(d.mode)

        mode = self._determine_mode(htemp_f, target_f, outdoor_f)
        fan  = self._determine_fan(htemp_f, target_f, outdoor_f)

        ceiling_forced = False
        pre_ceiling_mode = mode
        mode = self._apply_fan_ceiling(mode, htemp_f, current_mode_reported)
        if mode != pre_ceiling_mode:
            ceiling_forced = True
            fan = self._determine_fan(htemp_f, target_f, outdoor_f)

        self._track_ceiling_command_result(ceiling_forced, current_mode_reported, htemp_f)

        override_timeout = self._opt(CONF_OVERRIDE_TIMEOUT, DEFAULT_OVERRIDE_TIMEOUT)
        if override_timeout > 0 and self._detect_manual_override(
            current_mode_reported, d.fan_rate, d.target_temp
        ):
            self._override_until = time.monotonic() + override_timeout
            _LOGGER.info("Manual override detected — pausing for %ds", override_timeout)

        delta_now = abs(htemp_f - target_f)
        safety_delta = self._opt(CONF_SAFETY_OVERRIDE_DELTA, DEFAULT_SAFETY_OVERRIDE_DELTA)

        override_active = time.monotonic() < self._override_until
        is_deescalation  = mode == MODE_FAN

        if ceiling_forced:
            if override_active:
                _LOGGER.warning("Ceiling override bypassing active manual-override pause")
                self._override_until = 0.0
        elif override_active and not is_deescalation:
            if delta_now >= safety_delta:
                _LOGGER.warning(
                    "Safety bypass triggered — delta=%.1f°F >= %.1f°F, clearing override pause",
                    delta_now, safety_delta,
                )
                self._override_until = 0.0
            else:
                _LOGGER.debug(
                    "Override active (%.0fs remaining, delta=%.1f°F) — blocking escalation to %s",
                    self._override_until - time.monotonic(), delta_now, mode,
                )
                self._notify_entities()
                return
        elif override_active and is_deescalation:
            _LOGGER.debug("Override active but de-escalating to fan-only — always allowed")

        self._record_cycle(outdoor_f, htemp_f, target_f, mode)

        target_c  = (target_f - 32) * 5 / 9
        stemp_c   = round(target_c * 2) / 2
        stemp_str = str(stemp_c)

        _LOGGER.info(
            "htemp=%.1f°F | outdoor=%.1f°F | target=%.1f°F | delta=%+.1f°F | mode=%s | fan=%s%s",
            htemp_f, outdoor_f, target_f, htemp_f - target_f, mode, fan,
            " | CEILING FORCED" if ceiling_forced else "",
        )

        current_fan     = d.fan_rate
        current_stemp_c = d.target_temp
        already_ok = (
            current_mode_reported == mode
            and current_fan == fan
            and abs(current_stemp_c - stemp_c) < 0.25
        )
        if already_ok:
            _LOGGER.debug("AC already at desired state — no command needed")
            self.last_mode = mode
            self._notify_entities()
            return

        mode_changed = mode != self._last_commanded_mode
        now = time.monotonic()
        bypass_switch_guard = is_deescalation or ceiling_forced or delta_now >= safety_delta
        if mode_changed and not bypass_switch_guard and (now - self._last_mode_switch_at) < self._opt(
            CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN
        ):
            _LOGGER.debug(
                "Mode switch to %s suppressed — %.0fs since last switch",
                mode, now - self._last_mode_switch_at,
            )
            self._notify_entities()
            return
        elif mode_changed and bypass_switch_guard and (now - self._last_mode_switch_at) < self._opt(
            CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN
        ):
            _LOGGER.warning(
                "Mode-switch guard bypassed (ceiling=%s, safety_delta=%s)",
                ceiling_forced, delta_now >= safety_delta,
            )

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

        await self.coordinator.async_request_refresh()

        self._last_commanded_mode  = mode
        self._last_commanded_fan   = fan
        self._last_commanded_stemp = target_f
        if mode_changed:
            self._last_mode_switch_at = now
        self.last_mode = mode
        _LOGGER.info("Command sent — mode=%s fan=%s stemp=%.1f°C", mode, fan, stemp_c)
        self._notify_entities()
