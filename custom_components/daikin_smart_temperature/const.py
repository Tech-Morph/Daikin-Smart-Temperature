"""Constants for Daikin Smart Temperature."""

DOMAIN = "daikin_smart_temperature"
DAIKIN_DOMAIN = "daikin_comfort_control"

CONF_DEVICE_ID = "device_id"
CONF_TARGET_TEMP = "target_temp"
CONF_TOLERANCE = "tolerance"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_POLL_INTERVAL = "poll_interval"
CONF_MODE_SWITCH_MIN = "mode_switch_min"
CONF_OVERRIDE_TIMEOUT = "override_timeout"
CONF_LEARNING_ENABLED = "learning_enabled"

CONF_MORNING_OFFSET = "morning_offset"
CONF_DAY_OFFSET = "day_offset"
CONF_EVENING_OFFSET = "evening_offset"
CONF_NIGHT_OFFSET = "night_offset"

CONF_FAN_CLOSE_DELTA = "fan_close_delta"
CONF_FAN_MID_DELTA = "fan_mid_delta"

CONF_ALLOW_COOL = "allow_cool"
CONF_ALLOW_HEAT = "allow_heat"
CONF_ALLOW_FAN_ONLY = "allow_fan_only"
CONF_MAX_FAN_MODE = "max_fan_mode"
CONF_SEASON_MODE = "season_mode"
CONF_SUMMER_HEAT_MIN_TEMP = "summer_heat_min_temp"
CONF_SUMMER_HEAT_NIGHT_ONLY = "summer_heat_night_only"

CONF_OUTDOOR_HEAT_MAX = "outdoor_heat_max"
CONF_PRECOOL_ENABLED = "precool_enabled"
CONF_PRECOOL_RISE_THRESHOLD = "precool_rise_threshold"
CONF_PRECOOL_TOLERANCE_CUT = "precool_tolerance_cut"
CONF_LEARNING_LOG_ENABLED = "learning_log_enabled"
CONF_LEARNING_LOG_SIZE = "learning_log_size"

# Safety bypass — forces correction even if a manual-override pause is active
CONF_SAFETY_OVERRIDE_DELTA = "safety_override_delta"

FAN_RATE_AUTO = "A"
FAN_RATE_LOW = "2"
FAN_RATE_MEDIUM = "3"
FAN_RATE_HIGH = "4"

MODE_COOL = "3"
MODE_HEAT = "4"
MODE_FAN = "6"

FAN_CAP_AUTO = "auto"
FAN_CAP_LOW = "low"
FAN_CAP_MEDIUM = "medium"
FAN_CAP_HIGH = "high"

SEASON_NORMAL = "normal"
SEASON_SUMMER = "summer"

DEFAULT_TARGET_TEMP = 70.0
DEFAULT_TOLERANCE = 2.0
DEFAULT_MIN_TEMP = 64.0
DEFAULT_MAX_TEMP = 85.0
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MODE_SWITCH_MIN = 300
DEFAULT_OVERRIDE_TIMEOUT = 1800
DEFAULT_LEARNING_ENABLED = True

DEFAULT_MORNING_OFFSET = 0.0
DEFAULT_DAY_OFFSET = 1.0
DEFAULT_EVENING_OFFSET = 1.0
DEFAULT_NIGHT_OFFSET = -2.0

DEFAULT_FAN_CLOSE_DELTA = 2.0
DEFAULT_FAN_MID_DELTA = 4.0

DEFAULT_ALLOW_COOL = True
DEFAULT_ALLOW_HEAT = True
DEFAULT_ALLOW_FAN_ONLY = True
DEFAULT_MAX_FAN_MODE = FAN_CAP_HIGH
DEFAULT_SEASON_MODE = SEASON_SUMMER
DEFAULT_SUMMER_HEAT_MIN_TEMP = 60.0
DEFAULT_SUMMER_HEAT_NIGHT_ONLY = True

DEFAULT_OUTDOOR_HEAT_MAX = 55.0
DEFAULT_PRECOOL_ENABLED = True
DEFAULT_PRECOOL_RISE_THRESHOLD = 3.0
DEFAULT_PRECOOL_TOLERANCE_CUT = 0.5
DEFAULT_LEARNING_LOG_ENABLED = True
DEFAULT_LEARNING_LOG_SIZE = 500

# If actual delta from target reaches/exceeds this, force correction even
# during an active manual-override pause. Prevents overnight runaway drift.
DEFAULT_SAFETY_OVERRIDE_DELTA = 4.0

# Outdoor trend tracking window, in seconds
OUTDOOR_TREND_WINDOW_SECONDS = 1800
