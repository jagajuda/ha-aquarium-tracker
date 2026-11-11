"""Constants for Aquarium Tracker."""

DOMAIN = "aquarium_tracker"
VERSION = "0.1.0"

CONFIG_DIR = "aquarium_tracker"
CONFIG_FILE = "tanks.yaml"
DB_FILE = "events.db"

PANEL_URL_PATH = "aquarium-tracker"
PANEL_COMPONENT = "aquarium-tracker-panel"
STATIC_URL = "/aquarium_tracker_static"

CONF_NOTIFY_SERVICE = "notify_service"

SIGNAL_UPDATED = f"{DOMAIN}_updated"

NOTIFY_ACTION_PREFIX = "AQT_SNOOZE|"
TICK_SECONDS = 30
