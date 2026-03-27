"""Constants for the Onkyo legacy integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "onkyo_legacy"
MANUFACTURER: Final = "Onkyo"
MODEL: Final = "PR-SC5507"
MODEL_TX8050: Final = "TX-8050"
DEFAULT_MODEL: Final = MODEL

CONF_SOURCES: Final = "sources"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_MAX_VOLUME: Final = "max_volume"
CONF_MODEL: Final = "model"

DEFAULT_NAME: Final = "Onkyo PR-SC5507"
TX8050_DEFAULT_NAME: Final = "Onkyo TX-8050"
DEFAULT_PORT: Final = 60128
DEFAULT_SCAN_INTERVAL: Final = 10
DEFAULT_MAX_VOLUME: Final = 80

SERVICE_REFRESH: Final = "refresh"
SERVICE_SET_LISTENING_MODE: Final = "set_listening_mode"
ATTR_LISTENING_MODE: Final = "listening_mode"

PLATFORMS: Final = ("media_player", "number", "select", "switch", "sensor")

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

TX8050_DEFAULT_SOURCES: Final = {
    "DVD -- BD/DVD": "dvd",
    "TAPE -- TV/TAPE": "tv/tape",
    "PHONO": "phono",
    "CD -- TV/CD": "tv/cd",
    "FM": "fm",
    "AM": "am",
    "INTERNET RADIO": "internet-radio",
    "NETWORK": "net",
}

PROFILE_DEFAULT_SOURCES: Final = {
    MODEL: DEFAULT_SOURCES,
    MODEL_TX8050: TX8050_DEFAULT_SOURCES,
}

PROFILE_DEFAULT_NAMES: Final = {
    MODEL: DEFAULT_NAME,
    MODEL_TX8050: TX8050_DEFAULT_NAME,
}

PROFILE_QUERYABLE_COMMANDS: Final = {
    MODEL: (
        "LMD",
        "DIM",
        "SLA",
        "LTN",
        "RAS",
        "ADQ",
        "ADV",
        "MOT",
        "TGA",
        "TGB",
        "TGC",
        "SLP",
        "CTL",
        "SWL",
        "IFA",
        "IFV",
    ),
    MODEL_TX8050: (
        "LMD",
        "DIM",
        "SLP",
        "TUN",
    ),
}
