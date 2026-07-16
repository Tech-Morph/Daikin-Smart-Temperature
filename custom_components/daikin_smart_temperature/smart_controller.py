"""
SmartTemperatureController

The core brain. Runs as an async HA background task, reads htemp/otemp
from the daikin_comfort_control coordinator (no extra API calls), and
issues set_control_info commands when the temperature drifts outside
the comfort band.

Key design decisions:
  - Uses hass.async_create_background_task() so HA does NOT track this
    coroutine as a setup dependency and the bootstrap watchdog never fires.
  - Sleep is at the END of the loop, so the first cycle runs immediately.
  - Always resolves the live ConfigEntry via entry_id so options changes
    take effect on the next poll without a reload.
  - Respects allow_cool / allow_heat / allow_fan_only toggles.
  - Caps fan speed at max_fan_mode.
  - Summer heat gate checks BOTH indoor temp and outdoor temp (from the
    Daikin unit's own otemp reading) and optionally restricts heating
    to nighttime only.
  - Pre-cooling: tracks outdoor temp over a rolling window; if it's
    rising quickly, tightens the tolerance band.
  - Rolling in-memory learning log: records each cycle's outdoor/indoor/
    target/mode for future empirical tuning.
  - Manual-override detection IGNORES setpoint comparisons while the
    last commanded mode was fan-only, since Daikin units frequently
    report a stale/meaningless stemp value in that mode — this was
    causing false-positive overrides that silently re-armed forever
    and blocked _determine_mode() from ever running again.
  - Safety bypass: even if an override pause IS active, a delta from
    target that reaches safety_override_delta forces the pause to
    clear immediately so the unit can correct. Also bypasses the
    mode-switch short-cycle guard under the same condition. This is
    the hard backstop against any future overnight runaway.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
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
    CONF_ALLOW_COOL, CONF_ALLOW_HEAT, CONF_ALLOW_FAN_ONLY,
    CONF_MAX_FAN_MODE, CONF_SEASON_MODE,
    CONF_SUMMER_HEAT_MIN_TEMP, CONF_SUMMER_HEAT_NIGHT_ONLY,
    CONF_OUTDOOR_HEAT_MAX, CONF_PRECOOL_ENABLED,
    CONF_PRECOOL_RISE_THRESHOLD, CONF_PRECOOL_TOLERANCE_CUT,
    CONF_LEARNING_LOG_ENABLED, CONF_LEARNING_LOG_SIZE,
    CONF_SAFETY_OVERRIDE_DELTA,
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

        # Initialize from real saved options immediately so sensors don't
        # show a placeholder "72°F / unknown" before the first cycle runs.
        self.current_target_f: float = self._target_temp_f()
        self.last_mode: str = "unknown"

        # Outdoor trend tracking: (timestamp, outdoor_temp_f)
        self._outdoor_history: deque[tuple[float, float]] = deque()

        # Rolling in-memory learning log (FIFO)
        self._cycle_log: deque[dict] = deque()

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
            "Options reloaded — new target=%.1f°F, max=%.1f°F",
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

    def _is_night_slot(self) -> bool:
        now = datetime.now().time()
        return now >= dtime(22, 0) or now < dtime(6, 0)

    # ------------------------------------------------------------------ outdoor trend / pre-cooling

    def _record_outdoor_sample(self, outdoor_temp_f: float) -> None:
        """Track outdoor temp over a rolling window for pre-cool detection."""
        now = time.monotonic()
        self._outdoor_history.append((now, outdoor_temp_f))
        cutoff = now - OUTDOOR_TREND_WINDOW_SECONDS
        while self._outdoor_history and self._outdoor_history[0][0] < cutoff:
            self._outdoor_history.popleft()

    def _outdoor_rising_fast(self) -> bool:
        """True if outdoor temp has risen more than precool_rise_threshold
        within the tracking window."""
        if len(self._outdoor_history) < 2:
            return False
        oldest_temp = self._outdoor_history[0][1]
        newest_temp = self._outdoor_history[-1][1]
        rise = newest_temp - oldest_temp
        threshold = self._opt(CONF_PRECOOL_RISE_THRESHOLD, DEFAULT_PRECOOL_RISE_THRESHOLD)
        return rise >= threshold

    def _effective_tolerance(self) -> float:
        """Base tolerance, tightened when outdoor heat is climbing fast."""
        tol = self._opt(CONF_TOLERANCE, DEFAULT_TOLERANCE)
        if not self._opt(CONF_PRECOOL_ENABLED, DEFAULT_PRECOOL_ENABLED):
            return tol
        if self._outdoor_rising_fast():
            cut = self._opt(CONF_PRECOOL_TOLERANCE_CUT, DEFAULT_PRECOOL_TOLERANCE_CUT)
            tightened = max(0.25, tol - cut)
            _LOGGER.debug(
                "Pre-cool active — outdoor rising fast, tolerance %.1f -> %.1f",
                tol, tightened,
            )
            return tightened
        return tol

    # ------------------------------------------------------------------ mode / fan logic

    def _heat_allowed_now(self, htemp_f: float, outdoor_temp_f: float) -> bool:
        """Decide whether heating is permitted right now.

        In summer season mode, heating only kicks in once BOTH:
          - the house has dropped to/below summer_heat_min_temp, AND
          - it's actually cold outside (<= outdoor_heat_max)
        and optionally only during the night slot.
        """
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
        """Clamp the desired fan rate to the user's max_fan_mode setting."""
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

    def _determine_mode(self, htemp_f: float, target_f: float, outdoor_temp_f: float) -> str:
        delta = htemp_f - target_f
        tol   = self._effective_tolerance()

        allow_cool = self._opt(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL)

        if abs(delta) <= tol:
            return MODE_FAN

        if delta > tol:
            return MODE_COOL if allow_cool else MODE_FAN

        # delta < -tol -> house is colder than target
        if self._heat_allowed_now(htemp_f, outdoor_temp_f):
            return MODE_HEAT

        return MODE_FAN

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
        """Detect if the user changed the AC manually (app/remote).

        IMPORTANT: while the last commanded mode was fan-only, the
        setpoint (stemp) reported by the unit is frequently stale or
        firmware-defaulted and meaningless. Comparing it in that state
        was causing false-positive overrides on nearly every poll,
        which continuously re-armed the override pause and blocked
        _determine_mode() from ever running — this was the root cause
        of the overnight fan-mode-stuck-at-74°F issue.
        """
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

    # ------------------------------------------------------------------ learning log

    def _record_cycle(self, outdoor_f: float, htemp_f: float, target_f: float, mode: str) -> None:
        """Append a snapshot to the rolling learning log. Pure data
        collection for now — no decisions are made from this yet."""
        if not self._opt(CONF_LEARNING_LOG_ENABLED, DEFAULT_LEARNING_LOG_ENABLED):
            return

        max_size = int(self._opt(CONF_LEARNING_LOG_SIZE, DEFAULT_LEARNING_LOG_SIZE))
        self._cycle_log.append({
            "ts": time.time(),
            "outdoor_f": outdoor_f,
            "indoor_f": htemp_f,
            "target_f": target_f,
            "mode": mode,
        })
        while len(self._cycle_log) > max_size:
            self._cycle_log.popleft()

        if len(self._cycle_log) % 50 == 0:
            _LOGGER.debug("Learning log size: %d entries", len(self._cycle_log))

    @property
    def learning_log_size(self) -> int:
        """Exposed for a future diagnostic sensor."""
        return len(self._cycle_log)

    # ------------------------------------------------------------------ main loop

    async def _loop(self) -> None:
        """Main control loop. Sleep is at the END so the first cycle runs
        immediately on startup and never blocks the bootstrap phase."""
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
            _LOGGER.debug("otemp unavailable this cycle — using htemp as fallback for outdoor-aware logic")

        self._record_outdoor_sample(outdoor_f)

        target_f = self._target_temp_f()
        self.current_target_f = target_f

        _LOGGER.debug(
            "season=%s allow_cool=%s allow_heat=%s allow_fan_only=%s max_fan=%s outdoor=%.1f°F precool_active=%s",
            self._opt(CONF_SEASON_MODE, DEFAULT_SEASON_MODE),
            self._opt(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL),
            self._opt(CONF_ALLOW_HEAT, DEFAULT_ALLOW_HEAT),
            self._opt(CONF_ALLOW_FAN_ONLY, DEFAULT_ALLOW_FAN_ONLY),
            self._opt(CONF_MAX_FAN_MODE, DEFAULT_MAX_FAN_MODE),
            outdoor_f,
            self._outdoor_rising_fast(),
        )

        override_timeout = self._opt(CONF_OVERRIDE_TIMEOUT, DEFAULT_OVERRIDE_TIMEOUT)
        if override_timeout > 0 and self._detect_manual_override(
            str(d.mode), d.fan_rate, d.target_temp
        ):
            self._override_until = time.monotonic() + override_timeout
            _LOGGER.info("Manual override detected — pausing for %ds", override_timeout)

        delta_now = abs(htemp_f - target_f)
        safety_delta = self._opt(CONF_SAFETY_OVERRIDE_DELTA, DEFAULT_SAFETY_OVERRIDE_DELTA)

        if time.monotonic() < self._override_until:
            if delta_now >= safety_delta:
                _LOGGER.warning(
                    "Safety bypass triggered — delta=%.1f°F >= %.1f°F, clearing override pause to force correction",
                    delta_now, safety_delta,
                )
                self._override_until = 0.0
            else:
                _LOGGER.debug(
                    "Override active (%.0fs remaining, delta=%.1f°F below safety threshold %.1f°F)",
                    self._override_until - time.monotonic(), delta_now, safety_delta,
                )
                return

        mode = self._determine_mode(htemp_f, target_f, outdoor_f)
        fan  = self._determine_fan(htemp_f, target_f, outdoor_f)

        self._record_cycle(outdoor_f, htemp_f, target_f, mode)

        target_c  = (target_f - 32) * 5 / 9
        stemp_c   = round(target_c * 2) / 2
        stemp_str = str(stemp_c)

        _LOGGER.info(
            "htemp=%.1f°F | outdoor=%.1f°F | target=%.1f°F | delta=%+.1f°F | mode=%s | fan=%s",
            htemp_f, outdoor_f, target_f, htemp_f - target_f, mode, fan,
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
            _LOGGER.debug("AC already at desired state — no command needed")
            self.last_mode = mode
            return

        mode_changed = mode != self._last_commanded_mode
        now = time.monotonic()
        if mode_changed and (now - self._last_mode_switch_at) < self._opt(
            CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN
        ):
            if delta_now >= safety_delta:
                _LOGGER.warning(
                    "Mode-switch guard bypassed — delta=%.1f°F >= safety threshold %.1f°F",
                    delta_now, safety_delta,
                )
            else:
                _LOGGER.debug(
                    "Mode switch to %s suppressed — %.0fs since last switch",
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
        _LOGGER.info("Command sent — mode=%s fan=%s stemp=%.1f°C", mode, fan, stemp_c)
