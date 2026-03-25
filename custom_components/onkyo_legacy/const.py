"""Constants for the Onkyo legacy integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "onkyo_legacy"

CONF_SOURCES: Final = "sources"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_MAX_VOLUME: Final = "max_volume"

DEFAULT_NAME: Final = "Onkyo PR-SC5507"
DEFAULT_PORT: Final = 60128
DEFAULT_SCAN_INTERVAL: Final = 10
DEFAULT_MAX_VOLUME: Final = 80

SERVICE_REFRESH: Final = "refresh"
SERVICE_SET_LISTENING_MODE: Final = "set_listening_mode"
ATTR_LISTENING_MODE: Final = "listening_mode"

PLATFORMS: Final = ("media_player", "select", "switch")

DEFAULT_SOURCES: Final = {
    "TV": "tv",
    "Blu-ray": "dvd",
    "Game": "game",
    "Aux": "aux1",
    "PC": "pc",
    "CD": "cd",
    "FM": "fm",
    "AM": "am",
    "Phono": "phono",
    "Tuner": "tuner",
    "Network": "net",
    "USB": "usb",
}

SAFE_LISTENING_MODES: Final = (
    "stereo",
    "direct",
    "surround",
    "all-ch-stereo",
    "thx",
    "pure-audio",
)
