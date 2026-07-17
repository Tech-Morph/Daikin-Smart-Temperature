"""Config flow for Daikin Smart Temperature."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    DAIKIN_DOMAIN,
    CONF_DEVICE_ID,
    CONF_TARGET_TEMP,
    CONF_TOLERANCE,
    CONF_MIN_TEMP,
    CONF_MAX_TEMP,
    CONF_POLL_INTERVAL,
    CONF_MODE_SWITCH_MIN,
    CONF_OVERRIDE_TIMEOUT,
    CONF_LEARNING_ENABLED,
    CONF_MORNING_OFFSET,
    CONF_DAY_OFFSET,
    CONF_EVENING_OFFSET,
    CONF_NIGHT_OFFSET,
    CONF_FAN_CLOSE_DELTA,
    CONF_FAN_MID_DELTA,
    CONF_ALLOW_COOL,
    CONF_ALLOW_HEAT,
    CONF_ALLOW_FAN_ONLY,
    CONF_MAX_FAN_MODE,
    CONF_SEASON_MODE,
    CONF_SUMMER_HEAT_MIN_TEMP,
    CONF_SUMMER_HEAT_NIGHT_ONLY,
    CONF_OUTDOOR_HEAT_MAX,
    CONF_PRECOOL_ENABLED,
    CONF_PRECOOL_RISE_THRESHOLD,
    CONF_PRECOOL_TOLERANCE_CUT,
    CONF_LEARNING_LOG_ENABLED,
    CONF_LEARNING_LOG_SIZE,
    CONF_SAFETY_OVERRIDE_DELTA,
    CONF_FAN_CEILING_ENABLED,
    CONF_FAN_CEILING_TEMP,
    CONF_WEATHER_ENTITY,
    CONF_FORECAST_PRECOOL_ENABLED,
    CONF_FORECAST_HIGH_THRESHOLD,
    CONF_FORECAST_PRECOOL_TOLERANCE_CUT,
    CONF_FORECAST_CHECK_INTERVAL_CYCLES,
    DEFAULT_TARGET_TEMP,
    DEFAULT_TOLERANCE,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_MODE_SWITCH_MIN,
    DEFAULT_OVERRIDE_TIMEOUT,
    DEFAULT_LEARNING_ENABLED,
    DEFAULT_MORNING_OFFSET,
    DEFAULT_DAY_OFFSET,
    DEFAULT_EVENING_OFFSET,
    DEFAULT_NIGHT_OFFSET,
    DEFAULT_FAN_CLOSE_DELTA,
    DEFAULT_FAN_MID_DELTA,
    DEFAULT_ALLOW_COOL,
    DEFAULT_ALLOW_HEAT,
    DEFAULT_ALLOW_FAN_ONLY,
    DEFAULT_MAX_FAN_MODE,
    DEFAULT_SEASON_MODE,
    DEFAULT_SUMMER_HEAT_MIN_TEMP,
    DEFAULT_SUMMER_HEAT_NIGHT_ONLY,
    DEFAULT_OUTDOOR_HEAT_MAX,
    DEFAULT_PRECOOL_ENABLED,
    DEFAULT_PRECOOL_RISE_THRESHOLD,
    DEFAULT_PRECOOL_TOLERANCE_CUT,
    DEFAULT_LEARNING_LOG_ENABLED,
    DEFAULT_LEARNING_LOG_SIZE,
    DEFAULT_SAFETY_OVERRIDE_DELTA,
    DEFAULT_FAN_CEILING_ENABLED,
    DEFAULT_FAN_CEILING_TEMP,
    DEFAULT_FORECAST_PRECOOL_ENABLED,
    DEFAULT_FORECAST_HIGH_THRESHOLD,
    DEFAULT_FORECAST_PRECOOL_TOLERANCE_CUT,
    DEFAULT_FORECAST_CHECK_INTERVAL_CYCLES,
    FAN_CAP_AUTO,
    FAN_CAP_LOW,
    FAN_CAP_MEDIUM,
    FAN_CAP_HIGH,
    SEASON_NORMAL,
    SEASON_SUMMER,
)


class DaikinSmartTempConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}

        daikin_data = self.hass.data.get(DAIKIN_DOMAIN, {})
        devices: dict[str, str] = {}
        for _, coordinators in daikin_data.items():
            for coord in coordinators:
                devices[coord.device_id] = coord.device_name

        if not devices:
            return self.async_abort(reason="no_daikin_devices")

        if user_input is not None:
            await self.async_set_unique_id(f"{DOMAIN}_{user_input[CONF_DEVICE_ID]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Smart Temp — {devices[user_input[CONF_DEVICE_ID]]}",
                data={CONF_DEVICE_ID: user_input[CONF_DEVICE_ID]},
            )

        schema = vol.Schema({
            vol.Required(CONF_DEVICE_ID): vol.In(devices),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DaikinSmartTempOptionsFlow()


class DaikinSmartTempOptionsFlow(OptionsFlow):
    """Options flow."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_MIN_TEMP] > user_input[CONF_MAX_TEMP]:
                errors["base"] = "invalid_temp_range"
            elif not (
                user_input[CONF_ALLOW_COOL]
                or user_input[CONF_ALLOW_HEAT]
                or user_input[CONF_ALLOW_FAN_ONLY]
            ):
                errors["base"] = "at_least_one_hvac_mode"
            else:
                return self.async_create_entry(title="", data=user_input)

        o = self.config_entry.options

        schema = vol.Schema({
            vol.Optional(CONF_TARGET_TEMP, default=o.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)): vol.Coerce(float),
            vol.Optional(CONF_TOLERANCE, default=o.get(CONF_TOLERANCE, DEFAULT_TOLERANCE)): vol.Coerce(float),
            vol.Optional(CONF_MIN_TEMP, default=o.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)): vol.Coerce(float),
            vol.Optional(CONF_MAX_TEMP, default=o.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)): vol.Coerce(float),

            vol.Optional(CONF_ALLOW_COOL, default=o.get(CONF_ALLOW_COOL, DEFAULT_ALLOW_COOL)): bool,
            vol.Optional(CONF_ALLOW_HEAT, default=o.get(CONF_ALLOW_HEAT, DEFAULT_ALLOW_HEAT)): bool,
            vol.Optional(CONF_ALLOW_FAN_ONLY, default=o.get(CONF_ALLOW_FAN_ONLY, DEFAULT_ALLOW_FAN_ONLY)): bool,
            vol.Optional(CONF_MAX_FAN_MODE, default=o.get(CONF_MAX_FAN_MODE, DEFAULT_MAX_FAN_MODE)): vol.In(
                [FAN_CAP_AUTO, FAN_CAP_LOW, FAN_CAP_MEDIUM, FAN_CAP_HIGH]
            ),

            vol.Optional(CONF_SEASON_MODE, default=o.get(CONF_SEASON_MODE, DEFAULT_SEASON_MODE)): vol.In(
                [SEASON_NORMAL, SEASON_SUMMER]
            ),
            vol.Optional(
                CONF_SUMMER_HEAT_MIN_TEMP,
                default=o.get(CONF_SUMMER_HEAT_MIN_TEMP, DEFAULT_SUMMER_HEAT_MIN_TEMP),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_SUMMER_HEAT_NIGHT_ONLY,
                default=o.get(CONF_SUMMER_HEAT_NIGHT_ONLY, DEFAULT_SUMMER_HEAT_NIGHT_ONLY),
            ): bool,
            vol.Optional(
                CONF_OUTDOOR_HEAT_MAX,
                default=o.get(CONF_OUTDOOR_HEAT_MAX, DEFAULT_OUTDOOR_HEAT_MAX),
            ): vol.Coerce(float),

            # Layer 1 — hard fan-only ceiling
            vol.Optional(
                CONF_FAN_CEILING_ENABLED,
                default=o.get(CONF_FAN_CEILING_ENABLED, DEFAULT_FAN_CEILING_ENABLED),
            ): bool,
            vol.Optional(
                CONF_FAN_CEILING_TEMP,
                default=o.get(CONF_FAN_CEILING_TEMP, DEFAULT_FAN_CEILING_TEMP),
            ): vol.Coerce(float),

            # Layer 2 — forecast-aware pre-cooling
            vol.Optional(
                CONF_WEATHER_ENTITY,
                default=o.get(CONF_WEATHER_ENTITY, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(
                CONF_FORECAST_PRECOOL_ENABLED,
                default=o.get(CONF_FORECAST_PRECOOL_ENABLED, DEFAULT_FORECAST_PRECOOL_ENABLED),
            ): bool,
            vol.Optional(
                CONF_FORECAST_HIGH_THRESHOLD,
                default=o.get(CONF_FORECAST_HIGH_THRESHOLD, DEFAULT_FORECAST_HIGH_THRESHOLD),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_FORECAST_PRECOOL_TOLERANCE_CUT,
                default=o.get(CONF_FORECAST_PRECOOL_TOLERANCE_CUT, DEFAULT_FORECAST_PRECOOL_TOLERANCE_CUT),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_FORECAST_CHECK_INTERVAL_CYCLES,
                default=o.get(CONF_FORECAST_CHECK_INTERVAL_CYCLES, DEFAULT_FORECAST_CHECK_INTERVAL_CYCLES),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),

            vol.Optional(
                CONF_PRECOOL_ENABLED,
                default=o.get(CONF_PRECOOL_ENABLED, DEFAULT_PRECOOL_ENABLED),
            ): bool,
            vol.Optional(
                CONF_PRECOOL_RISE_THRESHOLD,
                default=o.get(CONF_PRECOOL_RISE_THRESHOLD, DEFAULT_PRECOOL_RISE_THRESHOLD),
            ): vol.Coerce(float),
            vol.Optional(
                CONF_PRECOOL_TOLERANCE_CUT,
                default=o.get(CONF_PRECOOL_TOLERANCE_CUT, DEFAULT_PRECOOL_TOLERANCE_CUT),
            ): vol.Coerce(float),

            vol.Optional(
                CONF_SAFETY_OVERRIDE_DELTA,
                default=o.get(CONF_SAFETY_OVERRIDE_DELTA, DEFAULT_SAFETY_OVERRIDE_DELTA),
            ): vol.Coerce(float),

            vol.Optional(
                CONF_LEARNING_LOG_ENABLED,
                default=o.get(CONF_LEARNING_LOG_ENABLED, DEFAULT_LEARNING_LOG_ENABLED),
            ): bool,
            vol.Optional(
                CONF_LEARNING_LOG_SIZE,
                default=o.get(CONF_LEARNING_LOG_SIZE, DEFAULT_LEARNING_LOG_SIZE),
            ): vol.All(vol.Coerce(int), vol.Range(min=50, max=5000)),

            vol.Optional(CONF_LEARNING_ENABLED, default=o.get(CONF_LEARNING_ENABLED, DEFAULT_LEARNING_ENABLED)): bool,
            vol.Optional(CONF_MORNING_OFFSET, default=o.get(CONF_MORNING_OFFSET, DEFAULT_MORNING_OFFSET)): vol.Coerce(float),
            vol.Optional(CONF_DAY_OFFSET, default=o.get(CONF_DAY_OFFSET, DEFAULT_DAY_OFFSET)): vol.Coerce(float),
            vol.Optional(CONF_EVENING_OFFSET, default=o.get(CONF_EVENING_OFFSET, DEFAULT_EVENING_OFFSET)): vol.Coerce(float),
            vol.Optional(CONF_NIGHT_OFFSET, default=o.get(CONF_NIGHT_OFFSET, DEFAULT_NIGHT_OFFSET)): vol.Coerce(float),

            vol.Optional(CONF_FAN_CLOSE_DELTA, default=o.get(CONF_FAN_CLOSE_DELTA, DEFAULT_FAN_CLOSE_DELTA)): vol.Coerce(float),
            vol.Optional(CONF_FAN_MID_DELTA, default=o.get(CONF_FAN_MID_DELTA, DEFAULT_FAN_MID_DELTA)): vol.Coerce(float),

            vol.Optional(CONF_POLL_INTERVAL, default=o.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)): vol.All(vol.Coerce(int), vol.Range(min=30)),
            vol.Optional(CONF_MODE_SWITCH_MIN, default=o.get(CONF_MODE_SWITCH_MIN, DEFAULT_MODE_SWITCH_MIN)): vol.All(vol.Coerce(int), vol.Range(min=60)),
            vol.Optional(CONF_OVERRIDE_TIMEOUT, default=o.get(CONF_OVERRIDE_TIMEOUT, DEFAULT_OVERRIDE_TIMEOUT)): vol.All(vol.Coerce(int), vol.Range(min=0)),
        })

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
