"""
Configuration module for Synology Surveillance Station to Telegram bridge.
All settings are read from environment variables.
"""

import os
import sys
import logging

# ============================================================================
# LOGGING
# ============================================================================

LOG_FORMAT = (
    "%(asctime)s - [%(levelname)s] - %(name)s"
    " - (%(filename)s).%(funcName)s(%(lineno)d) - %(message)s"
)

_log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _log_level_str, logging.INFO)


def setup_logger(name: str) -> logging.Logger:
    """Return a configured logger. Guard against duplicate handlers on reload."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(_LOG_LEVEL)
    return logger


# ============================================================================
# REQUIRED ENVIRONMENT VARIABLES
# ============================================================================

# SYNO_PORT is intentionally omitted: it has a sensible default (5000)
REQUIRED_ENV_VARS = [
    "TG_CHAT_ID",
    "TG_TOKEN",
    "SYNO_IP",
    "SYNO_LOGIN",
    "SYNO_PASS",
]

# ============================================================================
# OPTIONAL ENVIRONMENT VARIABLES (with defaults)
# ============================================================================

OPTIONAL_ENV_VARS = {
    "SYNO_PORT": "5000",
    "SYNO_OTP": None,
    "CONFIG_FILE": "/bot/syno_cam_config.json",
    "VIDEO_FILE": "/bot/temp.mp4",
    "VIDEO_SEGMENT_DURATION": 10000,   # ms
    "WEBHOOK_TIMEOUT": 5,              # seconds – wait before fetching video
    "API_TIMEOUT": 30,                 # seconds – Synology request timeout
    "LOG_LEVEL": "INFO",
    "GUNICORN_WORKERS": 1,             # must stay 1 – state is in-process
    "GUNICORN_TIMEOUT": 120,
}

# ============================================================================
# TELEGRAM
# ============================================================================

TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID")
TELEGRAM_TOKEN = os.environ.get("TG_TOKEN")

# ============================================================================
# SYNOLOGY
# ============================================================================

SYNOLOGY_IP = os.environ.get("SYNO_IP")
SYNOLOGY_PORT = os.environ.get("SYNO_PORT", OPTIONAL_ENV_VARS["SYNO_PORT"])
SYNOLOGY_LOGIN = os.environ.get("SYNO_LOGIN")
SYNOLOGY_PASSWORD = os.environ.get("SYNO_PASS")
SYNOLOGY_OTP = os.environ.get("SYNO_OTP")
SYNOLOGY_URL = f"http://{SYNOLOGY_IP}:{SYNOLOGY_PORT}/webapi/entry.cgi"

# ============================================================================
# FILE PATHS
# ============================================================================

CONFIG_FILE = os.environ.get("CONFIG_FILE", OPTIONAL_ENV_VARS["CONFIG_FILE"])
VIDEO_FILE = os.environ.get("VIDEO_FILE", OPTIONAL_ENV_VARS["VIDEO_FILE"])

# ============================================================================
# TIMING
# ============================================================================

VIDEO_SEGMENT_DURATION = int(
    os.environ.get("VIDEO_SEGMENT_DURATION", OPTIONAL_ENV_VARS["VIDEO_SEGMENT_DURATION"])
)

WEBHOOK_TIMEOUT = int(
    os.environ.get("WEBHOOK_TIMEOUT", OPTIONAL_ENV_VARS["WEBHOOK_TIMEOUT"])
)

API_TIMEOUT = int(
    os.environ.get("API_TIMEOUT", OPTIONAL_ENV_VARS["API_TIMEOUT"])
)

# ============================================================================
# GUNICORN
# ============================================================================

GUNICORN_WORKERS = int(
    os.environ.get("GUNICORN_WORKERS", OPTIONAL_ENV_VARS["GUNICORN_WORKERS"])
)

GUNICORN_TIMEOUT = int(
    os.environ.get("GUNICORN_TIMEOUT", OPTIONAL_ENV_VARS["GUNICORN_TIMEOUT"])
)

# ============================================================================
# DEPENDENCIES (for auto-install fallback via utils.py)
# ============================================================================

DEPENDENCIES = {
    "telebot": "pyTelegramBotAPI",
    "flask": "flask",
    "requests": "requests",
}
