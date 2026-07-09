"""Constants for the Onkyo legacy integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "DOMAIN",
    "MANUFACTURER",
    "MODEL",
    "MODEL_TX8050",
    "DEFAULT_MODEL",
    "CONF_SOURCES",
    "CONF_SCAN_INTERVAL",
    "CONF_MAX_VOLUME",
    "CONF_MODEL",
    "DEFAULT_NAME",
    "TX8050_DEFAULT_NAME",
    "DEFAULT_PORT",
    "DEFAULT_SCAN_INTERVAL",
    "DEFAULT_MAX_VOLUME",
    "CONF_RETRIES",
    "DEFAULT_RETRIES",
    "CONF_STRICT_SOURCES",
    "DEFAULT_STRICT_SOURCES",
    "SERVICE_REFRESH",
    "SERVICE_SET_LISTENING_MODE",
    "ATTR_LISTENING_MODE",
    "PLATFORMS",
    "DEFAULT_SOURCES",
    "TX8050_DEFAULT_SOURCES",
    "ModelProfile",
    "PROFILES",
    "GENERIC_PROFILE",
]

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

CONF_RETRIES: Final = "retries"
DEFAULT_RETRIES: Final = 2

CONF_STRICT_SOURCES: Final = "strict_sources"
DEFAULT_STRICT_SOURCES: Final = True

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

@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Frozen profile for a known Onkyo receiver model."""

    model: str
    default_name: str
    default_sources: dict[str, str]
    queryable_commands: tuple[str, ...]
    supported_zones: tuple[str, ...]
    max_volume: int = 80


PROFILES: dict[str, ModelProfile] = {
    MODEL: ModelProfile(
        model=MODEL,
        default_name=DEFAULT_NAME,
        default_sources=DEFAULT_SOURCES,
        queryable_commands=(
            "LMD", "DIM", "SLA", "LTN", "RAS", "ADQ", "ADV", "MOT",
            "TGA", "TGB", "TGC", "SLP", "CTL", "SWL", "IFA", "IFV",
        ),
        supported_zones=("main",),
        max_volume=80,
    ),
    MODEL_TX8050: ModelProfile(
        model=MODEL_TX8050,
        default_name=TX8050_DEFAULT_NAME,
        default_sources=TX8050_DEFAULT_SOURCES,
        queryable_commands=("LMD", "DIM", "SLP", "TUN"),
        supported_zones=("main",),
        max_volume=80,
    ),
}

GENERIC_PROFILE = ModelProfile(
    model="GENERIC",
    default_name="Onkyo Receiver",
    default_sources={},
    queryable_commands=(
        "PWR", "MVL", "AMT", "SLI",  # core
        "LMD", "DIM", "SLA", "LTN", "RAS", "ADQ", "ADV", "MOT",  # main optional
        "TGA", "TGB", "TGC", "SLP", "CTL", "SWL",  # switches/levels
        "IFA", "IFV", "RES", "HDO",  # information
        "TUN", "PRS",  # tuner
    ),
    supported_zones=("main",),
    max_volume=80,
)
