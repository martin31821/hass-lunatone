"""Constants for the Lunatone DALI WLAN integration."""

DOMAIN = "lunatone_wlan"
MANUFACTURER = "Lunatone"

EVENT_MONITOR = f"{DOMAIN}_monitor"
EVENT_BUTTON = f"{DOMAIN}_button"
EVENT_OCCUPANCY = f"{DOMAIN}_occupancy"
EVENT_ILLUMINANCE = f"{DOMAIN}_illuminance"

ATTR_ADDRESS = "address"
ATTR_ANSWER = "answer"
ATTR_DATA = "data"
ATTR_GATEWAY = "gateway"
ATTR_GROUP = "group"
ATTR_INSTANCE = "instance"
ATTR_SCENE = "scene"
ATTR_SEND_TWICE = "send_twice"
ATTR_WAIT_FOR_ANSWER = "wait_for_answer"

CONF_CONFIG_ENTRY_ID = "config_entry_id"

# Button events offered as device triggers
TRIGGER_TYPES = (
    "short_press",
    "double_press",
    "long_press_start",
    "long_press_repeat",
    "long_press_stop",
    "pressed",
    "released",
)
