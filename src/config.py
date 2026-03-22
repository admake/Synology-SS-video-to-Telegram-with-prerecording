"""
Configuration for Synology SS → Telegram bridge.
All settings are read from environment variables at import time.
"""

from __future__ import annotations

import logging
import os
import sys

# ─── Logging ─────────────────────────────────────────────────────────────────

_LOG_LEVEL = getattr(
    logging,
    os.environ.get("LOG_LEVEL", "INFO").upper(),
    logging.INFO,
)

_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d) — %(message)s"
)


def setup_logger(name: str) -> logging.Logger:
    """Return a configured logger, guarding against duplicate handlers on reload."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(_LOG_LEVEL)
    return logger


# ─── Required env vars ───────────────────────────────────────────────────────

# SYNO_PORT intentionally omitted — has a safe default (5000).
REQUIRED_ENV_VARS = [
    "TG_CHAT_ID",
    "TG_TOKEN",
    "SYNO_IP",
    "SYNO_LOGIN",
    "SYNO_PASS",
]

# ─── Telegram ────────────────────────────────────────────────────────────────

TELEGRAM_CHAT_ID: str = os.environ.get("TG_CHAT_ID", "")
TELEGRAM_TOKEN: str = os.environ.get("TG_TOKEN", "")

# ─── Synology ────────────────────────────────────────────────────────────────

_SYNO_IP: str = os.environ.get("SYNO_IP", "")
_SYNO_PORT: str = os.environ.get("SYNO_PORT", "5000")
SYNOLOGY_LOGIN: str = os.environ.get("SYNO_LOGIN", "")
SYNOLOGY_PASSWORD: str = os.environ.get("SYNO_PASS", "")
SYNOLOGY_OTP: str = os.environ.get("SYNO_OTP", "")
SYNOLOGY_URL: str = f"http://{_SYNO_IP}:{_SYNO_PORT}/webapi/entry.cgi"

# ─── File paths ───────────────────────────────────────────────────────────────

CONFIG_FILE: str = os.environ.get("CONFIG_FILE", "/bot/syno_cam_config.json")

# Base path for per-camera temp video files.
# Actual paths are derived as: stem_<cam_id>.suffix  (e.g. temp_1.mp4)
VIDEO_FILE: str = os.environ.get("VIDEO_FILE", "/bot/temp.mp4")

# ─── Timing ───────────────────────────────────────────────────────────────────

# Length of each downloaded video segment (milliseconds).
VIDEO_SEGMENT_DURATION: int = int(os.environ.get("VIDEO_SEGMENT_DURATION", "10000"))

# Seconds to wait after receiving the webhook before fetching the recording.
# Gives Synology time to finish writing the file before we try to download it.
WEBHOOK_TIMEOUT: int = int(os.environ.get("WEBHOOK_TIMEOUT", "5"))

# Timeout for Synology JSON API calls (seconds).
API_TIMEOUT: int = int(os.environ.get("API_TIMEOUT", "30"))

# Timeout for streaming binary video downloads (seconds).
# Larger than API_TIMEOUT because video files can be multi-MB over a LAN.
VIDEO_DOWNLOAD_TIMEOUT: int = int(os.environ.get("VIDEO_DOWNLOAD_TIMEOUT", "90"))

# ─── Security ────────────────────────────────────────────────────────────────

# Optional shared secret that protects the /webhookcam endpoint.
# When set, every incoming webhook must supply the same value via the
# X-Webhook-Token request header (or ?token= query parameter).
# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", "")

# ─── Proxy ───────────────────────────────────────────────────────────────────

# Optional HTTP/SOCKS proxy for Telegram API calls.
# Examples: "http://host:3128"  or  "socks5://user:pass@host:1080"
TELEGRAM_PROXY: str = os.environ.get("TG_PROXY", "")
