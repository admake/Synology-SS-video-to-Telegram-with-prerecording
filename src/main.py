import pathlib
import time
import os
import json
import sys
import logging
from typing import Optional

# Import configuration
from config import (
    setup_logger,
    LOG_FORMAT,
    REQUIRED_ENV_VARS,
    OPTIONAL_ENV_VARS,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    SYNOLOGY_URL,
    SYNOLOGY_LOGIN,
    SYNOLOGY_PASSWORD,
    SYNOLOGY_OTP,
    CONFIG_FILE,
    VIDEO_FILE,
    VIDEO_SEGMENT_DURATION,
    WEBHOOK_TIMEOUT,
    API_TIMEOUT,
    DEPENDENCIES,
)

# Import utilities
from utils import ensure_module_installed

# Setup logger
log = setup_logger(__name__)

# Auto-install required modules (no-op when packages are already installed)
telebot = ensure_module_installed("telebot", DEPENDENCIES["telebot"])
flask_module = ensure_module_installed("flask", DEPENDENCIES["flask"])
requests = ensure_module_installed("requests", DEPENDENCIES["requests"])

Flask = flask_module.Flask
request = flask_module.request

# Resolve telebot rate-limit exception class once at import time.
# pyTelegramBotAPI exposes ApiTelegramException in the apihelper submodule.
try:
    _ApiTelegramException = telebot.apihelper.ApiTelegramException
except AttributeError:
    _ApiTelegramException = Exception  # safe fallback


# ============================================================================
# VALIDATION
# ============================================================================


def validate_required_env() -> None:
    """Exit with a clear message if any required env var is missing."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        for var in missing:
            log.error(f"{var} is not set. Please configure environment.")
        sys.exit(1)
    log.info("All required environment variables are set.")


validate_required_env()


# ============================================================================
# GLOBALS
# ============================================================================

chat_id = TELEGRAM_CHAT_ID
token = TELEGRAM_TOKEN
tg_bot = telebot.TeleBot(token)
log.info(f"Telegram bot initialized for chat {chat_id}")

syno_url = SYNOLOGY_URL
syno_login = SYNOLOGY_LOGIN
syno_pass = SYNOLOGY_PASSWORD
syno_otp = SYNOLOGY_OTP
config_file = CONFIG_FILE

# Synology session ID — always refreshed on startup, never loaded from disk.
syno_sid: Optional[str] = None

# Camera configuration (dict of cam_id -> camera info)
cam_load: dict = {}

# Per-camera offset tracking: cam_id -> {old_last_video_id, video_offset}
arr_cam_move: dict = {}


# ============================================================================
# TELEGRAM HELPERS
# ============================================================================


def send_cammessage(message: str) -> None:
    """Send a plain-text message to the configured Telegram chat."""
    try:
        tg_bot.send_message(chat_id, message)
        log.debug(f"Message sent to Telegram: {message[:60]}")
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")


def send_camvideo(videofile: str, cam_id: str) -> bool:
    """
    Send a video file to Telegram with the camera name as caption.
    Handles Telegram rate limiting (HTTP 429) with up to 3 retries.
    Returns True on success.
    """
    try:
        mycaption = f"Camera: {cam_load[cam_id]['SynoName']}"
    except KeyError:
        log.error(f"Camera {cam_id} not found in configuration")
        return False

    for attempt in range(3):
        try:
            with open(videofile, "rb") as video:
                tg_bot.send_video(chat_id, video, caption=mycaption)
            log.info(f"Video sent to Telegram for camera {cam_id}")
            return True
        except FileNotFoundError:
            log.error(f"Video file not found: {videofile}")
            return False
        except _ApiTelegramException as e:
            error_code = getattr(e, "error_code", None)
            if error_code == 429:
                result_json = getattr(e, "result_json", {}) or {}
                retry_after = result_json.get("parameters", {}).get("retry_after", 30)
                log.warning(
                    f"Telegram rate limit (429), retrying in {retry_after}s "
                    f"(attempt {attempt + 1}/3)"
                )
                time.sleep(retry_after)
            else:
                log.error(f"Telegram API error sending video: {e}")
                return False
        except Exception as e:
            log.error(f"Failed to send video to Telegram: {e}")
            return False

    log.error("Failed to send video after 3 rate-limit retries.")
    return False


# ============================================================================
# SYNOLOGY AUTHENTICATION
# ============================================================================


def syno_authenticate() -> bool:
    """
    Authenticate with Synology DSM and store the session ID in the global
    `syno_sid`.  Always called on startup — the SID from a previous run is
    expired and must never be reused.
    Returns True on success.
    """
    global syno_sid

    auth_params = {
        "api": "SYNO.API.Auth",
        "version": "7",
        "method": "login",
        "account": syno_login,
        "passwd": syno_pass,
        "session": "SurveillanceStation",
        "format": "cookie",
    }
    if syno_otp:
        auth_params["otp_code"] = syno_otp
        log.info("Using two-factor authentication (OTP)")

    try:
        response = requests.get(syno_url, params=auth_params, timeout=API_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if data.get("success"):
            syno_sid = data["data"]["sid"]
            log.info(f"Synology: authenticated (SID prefix: {syno_sid[:10]}...)")
            return True

        error_code = data.get("error", {}).get("code", "unknown")
        log.error(f"Synology authentication failed: error code {error_code}")
        return False

    except requests.exceptions.RequestException as e:
        log.error(f"Synology authentication request failed: {e}")
        return False
    except (KeyError, json.JSONDecodeError) as e:
        log.error(f"Failed to parse Synology auth response: {e}")
        return False


def syno_api_get(params: dict, _retry_auth: bool = True) -> Optional[dict]:
    """
    Make a Synology API GET request, injecting the current SID.

    If Synology returns a session-expiry error (codes 105, 106, 119) and
    `_retry_auth` is True, re-authenticates once and retries the call.
    Returns the parsed JSON dict, or None on network / parse error.
    """
    global syno_sid

    try:
        req_params = dict(params)
        req_params["_sid"] = syno_sid

        response = requests.get(syno_url, params=req_params, timeout=API_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if not data.get("success") and _retry_auth:
            # 105 = insufficient privilege / session expired
            # 106 = connection timed out
            # 119 = SID not found
            error_code = data.get("error", {}).get("code", 0)
            if error_code in (105, 106, 119):
                log.warning(
                    f"Synology session expired (code={error_code}), re-authenticating…"
                )
                if syno_authenticate():
                    return syno_api_get(params, _retry_auth=False)

        return data

    except requests.exceptions.Timeout:
        log.error(f"Synology API request timed out ({API_TIMEOUT}s)")
        return None
    except requests.exceptions.RequestException as e:
        log.error(f"Synology API request failed: {e}")
        return None
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Synology API response: {e}")
        return None


# ============================================================================
# CAMERA CONFIGURATION
# ============================================================================


def firstStart() -> None:
    """
    Fetch camera list from Synology and save to the config file.
    The session ID is NOT stored in the file — it is always re-fetched on
    startup via syno_authenticate().
    Requires syno_authenticate() to have been called first.
    """
    global cam_load

    log.info("Fetching camera configuration from Synology…")

    data = syno_api_get({
        "api": "SYNO.SurveillanceStation.Camera",
        "version": "9",
        "method": "List",
    })

    if not data or not data.get("success"):
        error_code = (data or {}).get("error", {}).get("code", "unknown")
        log.error(f"Failed to fetch cameras from Synology (error code: {error_code})")
        sys.exit(1)

    cameras = data.get("data", {}).get("cameras", [])
    if not cameras:
        log.error("No cameras found in Synology Surveillance Station")
        sys.exit(1)

    log.info(f"Found {len(cameras)} camera(s)")

    cam_data: dict = {}
    cam_conf_text = ""

    for camera in cameras:
        cam_id = str(camera["id"])
        cam_data[cam_id] = {
            "CamId": cam_id,
            "IP": camera.get("ip", "N/A"),
            "SynoName": camera.get("newName", "Unknown"),
            "Model": camera.get("model", "N/A"),
            "Vendor": camera.get("vendor", "N/A"),
        }
        cam_conf_text += (
            f"CamId: {cam_id}  "
            f"Name: {camera.get('newName', 'Unknown')}  "
            f"IP: {camera.get('ip', 'N/A')}\n"
        )

    cam_load = cam_data

    # Persist camera metadata (SID is intentionally excluded)
    try:
        pathlib.Path(config_file).parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(cam_data, f, indent=2)
        log.info(f"Camera configuration saved to {config_file}")
    except IOError as e:
        log.error(f"Failed to save camera config (non-fatal, using in-memory): {e}")

    send_cammessage(f"✅ Cameras config loaded:\n{cam_conf_text}")


def _init_cameras() -> None:
    """
    Load camera config from the JSON file if available and valid,
    otherwise fetch it from Synology.  Initialises arr_cam_move tracking.
    """
    global cam_load, arr_cam_move

    config_path = pathlib.Path(config_file)
    loaded_from_file = False

    if config_path.is_file() and config_path.stat().st_size > 0:
        try:
            with open(config_file) as f:
                raw = json.load(f)
            # Accept only entries that look like camera records.
            # Legacy configs stored SynologyAuthSid as a top-level key — skip it.
            cam_data = {
                k: v
                for k, v in raw.items()
                if isinstance(v, dict) and "CamId" in v
            }
            if cam_data:
                cam_load = cam_data
                loaded_from_file = True
                log.info(
                    f"Camera config loaded from {config_file}: "
                    f"{len(cam_load)} camera(s)"
                )
            else:
                log.warning("Config file has no valid camera entries, fetching from Synology")
        except (IOError, json.JSONDecodeError) as e:
            log.warning(f"Failed to load camera config from file: {e}")

    if not loaded_from_file:
        log.info("No valid camera config found locally, fetching from Synology…")
        firstStart()

    # Initialise per-camera offset tracking with correct types
    arr_cam_move.clear()
    for cam_id in cam_load:
        arr_cam_move[cam_id] = {
            "old_last_video_id": None,  # None means "no previous motion event"
            "video_offset": 0,          # int milliseconds
        }

    log.info(
        f"Tracking {len(arr_cam_move)} camera(s): {list(cam_load.keys())}"
    )


# ============================================================================
# MODULE-LEVEL INITIALISATION
# Runs when Gunicorn (or any WSGI server) imports this module.
# ============================================================================

# SID from any previous run is expired — always re-authenticate.
if not syno_authenticate():
    log.error("Failed to authenticate with Synology on startup. Exiting.")
    sys.exit(1)

# Load camera metadata from disk, or fetch it from Synology.
_init_cameras()


# ============================================================================
# SYNOLOGY API FUNCTIONS
# ============================================================================


def get_last_id_video(cam_id: str) -> Optional[str]:
    """Return the most recent recording ID for *cam_id*, or None on failure."""
    data = syno_api_get({
        "api": "SYNO.SurveillanceStation.Recording",
        "version": "6",
        "method": "List",
        "cameraIds": cam_id,
        "toTime": "0",
        "offset": "0",
        "limit": "1",
        "fromTime": "0",
    })

    if not data or not data.get("success"):
        log.error(f"Failed to get recording list for camera {cam_id}: {data}")
        return None

    try:
        recordings = data["data"]["recordings"]
        if not recordings:
            log.warning(f"No recordings found for camera {cam_id}")
            return None
        return str(recordings[0]["id"])
    except (KeyError, IndexError) as e:
        log.error(f"Failed to parse recording list for camera {cam_id}: {e}")
        return None


def get_last_video(video_id: str, offset: int, _retry: bool = True) -> bool:
    """
    Download a video segment from Synology and write it to VIDEO_FILE.

    Detects JSON error responses (Synology returns JSON instead of video bytes
    when the session has expired) and re-authenticates once before retrying.
    Returns True on success.
    """
    try:
        response = requests.get(
            syno_url,
            params={
                "api": "SYNO.SurveillanceStation.Recording",
                "version": "6",
                "method": "Download",
                "id": video_id,
                "mountId": "0",
                "offsetTimeMs": str(offset),
                "playTimeMs": str(VIDEO_SEGMENT_DURATION),
                "_sid": syno_sid,
            },
            allow_redirects=True,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()

        # If Synology returns JSON instead of video data, it's an error response.
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            err_data = {}
            try:
                err_data = response.json()
            except json.JSONDecodeError:
                pass
            error_code = err_data.get("error", {}).get("code", 0)
            if error_code in (105, 106, 119) and _retry:
                log.warning(
                    f"Session expired during video download (code={error_code}), "
                    f"re-authenticating…"
                )
                if syno_authenticate():
                    return get_last_video(video_id, offset, _retry=False)
            log.error(f"Synology returned JSON error during download: {err_data}")
            return False

        with open(VIDEO_FILE, "wb") as f:
            f.write(response.content)

        log.debug(
            f"Video downloaded: {len(response.content) / 1024:.1f} KB "
            f"(offset: {offset} ms)"
        )
        return True

    except requests.exceptions.RequestException as e:
        log.error(f"Failed to download video from Synology: {e}")
        return False
    except IOError as e:
        log.error(f"Failed to write video file {VIDEO_FILE}: {e}")
        return False


# ============================================================================
# FLASK APPLICATION
# ============================================================================

app = Flask(__name__)


@app.route("/webhookcam", methods=["POST"])
def webhookcam():
    """
    Handle motion-detection webhook from Synology Surveillance Station.

    Expected JSON body:
        {"idcam": "1"}

    Flow:
    1. Validate camera ID.
    2. Sleep WEBHOOK_TIMEOUT seconds to let Synology write the recording.
    3. Fetch the latest video ID for the camera.
    4. New motion event  → send first segment (offset 0) + alert message.
    5. Continuing motion → send next segment at the current offset.
    """
    if not request.json or "idcam" not in request.json:
        log.warning("Webhook rejected: missing 'idcam' field")
        return "Bad Request: missing idcam", 400

    cam_id = str(request.json["idcam"])

    if cam_id not in cam_load:
        log.warning(f"Webhook rejected: unknown camera {cam_id!r}")
        return f"Bad Request: unknown camera {cam_id}", 400

    if cam_id not in arr_cam_move:
        log.warning(f"Webhook rejected: camera {cam_id} not in tracking state")
        return "Bad Request: camera not tracked", 400

    try:
        cam_name = cam_load[cam_id]["SynoName"]
        log.info(
            f"Motion detected: camera {cam_id} ({cam_name}) "
            f"at {time.strftime('%d.%m.%Y %H:%M:%S')}"
        )

        time.sleep(WEBHOOK_TIMEOUT)

        last_video_id = get_last_id_video(cam_id)
        if last_video_id is None:
            log.error(f"Could not retrieve video ID for camera {cam_id}")
            return "Internal error: could not get video ID", 500

        if last_video_id != arr_cam_move[cam_id]["old_last_video_id"]:
            # New motion event — start from the beginning (includes pre-recording)
            arr_cam_move[cam_id]["old_last_video_id"] = last_video_id
            arr_cam_move[cam_id]["video_offset"] = 0
            send_cammessage(f"🔴 Motion detected: {cam_name}")
        else:
            # Continuing motion — advance to the next segment
            arr_cam_move[cam_id]["video_offset"] += VIDEO_SEGMENT_DURATION

        offset = arr_cam_move[cam_id]["video_offset"]

        if get_last_video(last_video_id, offset):
            send_camvideo(VIDEO_FILE, cam_id)
        else:
            log.error(
                f"Failed to download video for camera {cam_id} at offset {offset} ms"
            )
            return "Internal error: video download failed", 500

        return "success", 200

    except Exception as e:
        log.error(
            f"Unexpected error in webhook handler for camera {cam_id}: {e}",
            exc_info=True,
        )
        return "Internal Server Error", 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — returns current status as JSON."""
    return {
        "status": "healthy",
        "timestamp": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime()),
        "cameras": len(cam_load),
        "authenticated": syno_sid is not None,
    }, 200


# ============================================================================
# DEVELOPMENT ENTRY POINT
# In production Gunicorn is used; this block is only for local dev runs.
# ============================================================================

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Synology Surveillance Station → Telegram Bridge")
    log.info("Development mode — use Gunicorn for production")
    log.info(f"Webhook:      http://localhost:7878/webhookcam")
    log.info(f"Health check: http://localhost:7878/health")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=7878, debug=False)
