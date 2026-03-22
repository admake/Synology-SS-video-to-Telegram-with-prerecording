"""
Synology Surveillance Station → Telegram webhook bridge.

Receives motion-detection webhooks from Synology, downloads the
pre-recorded video segment and forwards it to a Telegram chat.

Architecture
────────────
• Flask/Gunicorn (gthread worker) receives the POST /webhookcam request.
• The handler validates the payload, spawns a daemon thread, and returns
  202 Accepted immediately — Gunicorn is never blocked waiting for video.
• The daemon thread (one per camera, serialised by a per-camera lock)
  sleeps WEBHOOK_TIMEOUT seconds, downloads the segment with streaming I/O,
  and sends it to Telegram with automatic retry on rate-limiting (429).
• Session management: _syno_authenticate() is always called on startup.
  If a Synology API call returns session-expired error (105 / 119), the
  module re-authenticates once and retries automatically.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import threading
import time
from typing import Optional

import requests
import telebot
from flask import Flask, request as flask_request

from config import (
    API_TIMEOUT,
    CONFIG_FILE,
    REQUIRED_ENV_VARS,
    SYNOLOGY_LOGIN,
    SYNOLOGY_OTP,
    SYNOLOGY_PASSWORD,
    SYNOLOGY_URL,
    TELEGRAM_CHAT_ID,
    TELEGRAM_PROXY,
    TELEGRAM_TOKEN,
    VIDEO_DOWNLOAD_TIMEOUT,
    VIDEO_FILE,
    VIDEO_SEGMENT_DURATION,
    WEBHOOK_SECRET,
    WEBHOOK_TIMEOUT,
    setup_logger,
)

log = setup_logger(__name__)

# ─── Validation ──────────────────────────────────────────────────────────────


def _validate_env() -> None:
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        for var in missing:
            log.error(f"Required env var not set: {var}")
        sys.exit(1)
    log.info("All required environment variables are present.")


_validate_env()

# ─── Module-level singletons ─────────────────────────────────────────────────

if TELEGRAM_PROXY:
    telebot.apihelper.proxy = {"https": TELEGRAM_PROXY}
    log.info(f"Telegram: using proxy {TELEGRAM_PROXY}")

_tg_bot = telebot.TeleBot(TELEGRAM_TOKEN)
log.info(f"Telegram bot initialised for chat {TELEGRAM_CHAT_ID}")

# Synology session ID.  Always re-fetched on startup — the previous SID
# is expired after a container restart and must never be reused.
_syno_sid: Optional[str] = None
_syno_sid_lock = threading.Lock()

# Serialises concurrent re-authentication attempts so that only one thread
# re-auths at a time.  Other threads wait and then reuse the fresh SID.
_syno_reauth_lock = threading.Lock()

# Camera metadata:   cam_id (str) → {CamId, SynoName, IP, …}
_cam_load: dict = {}
_cam_load_lock = threading.RLock()   # protects _cam_load dict replacement

# Per-camera motion state:  cam_id → {last_video_id, video_offset}
_cam_state: dict = {}
_cam_state_lock = threading.Lock()

# Per-camera processing lock — serialises concurrent webhooks for the same
# camera and makes non-blocking acquire a cheap "skip duplicate" check.
_cam_locks: dict[str, threading.Lock] = {}
_cam_locks_mu = threading.Lock()

# Emit the "no WEBHOOK_SECRET" warning at most once across all requests.
_secret_warned = False

# ─── Utilities ───────────────────────────────────────────────────────────────


def _cam_video_path(cam_id: str) -> str:
    """Derive a per-camera temp path from VIDEO_FILE (e.g. /bot/temp_1.mp4).

    Using a per-camera path prevents file collisions when two cameras fire
    at the same time.
    """
    p = pathlib.Path(VIDEO_FILE)
    return str(p.parent / f"{p.stem}_{cam_id}{p.suffix}")


def _get_cam_lock(cam_id: str) -> threading.Lock:
    """Return (creating if needed) the serialisation lock for *cam_id*."""
    with _cam_locks_mu:
        if cam_id not in _cam_locks:
            _cam_locks[cam_id] = threading.Lock()
        return _cam_locks[cam_id]


def _safe_remove(path: str) -> None:
    """Delete *path*, silently ignoring missing-file errors."""
    try:
        os.remove(path)
    except OSError:
        pass


# ─── Telegram ────────────────────────────────────────────────────────────────


def _send_message(text: str) -> None:
    """Send a plain-text message to the configured Telegram chat."""
    try:
        _tg_bot.send_message(TELEGRAM_CHAT_ID, text)
        log.debug(f"Telegram message sent: {text[:80]}")
    except Exception as exc:
        log.error(f"Failed to send Telegram message: {exc}")


def _send_video(video_path: str, cam_id: str) -> bool:
    """Send a video file to Telegram, retrying up to 3× on rate-limiting (429).

    Returns True on success.
    """
    with _cam_load_lock:
        cam_name = _cam_load.get(cam_id, {}).get("SynoName", f"Camera {cam_id}")
    caption = f"Camera: {cam_name}"

    for attempt in range(3):
        try:
            with open(video_path, "rb") as fh:
                _tg_bot.send_video(TELEGRAM_CHAT_ID, fh, caption=caption)
            log.info(f"Video sent to Telegram (camera {cam_id})")
            return True

        except FileNotFoundError:
            log.error(f"Video file missing: {video_path}")
            return False

        except telebot.apihelper.ApiTelegramException as exc:
            if exc.error_code == 429:
                retry_after = (
                    (getattr(exc, "result_json", None) or {})
                    .get("parameters", {})
                    .get("retry_after", 30)
                )
                log.warning(
                    f"Telegram 429 — retry in {retry_after}s "
                    f"(attempt {attempt + 1}/3)"
                )
                time.sleep(retry_after)
            else:
                log.error(f"Telegram API error (camera {cam_id}): {exc}")
                return False

        except Exception as exc:
            log.error(f"Failed to send video (camera {cam_id}): {exc}")
            return False

    log.error(f"Gave up sending video for camera {cam_id} after 3 attempts.")
    return False


# ─── Synology session management ─────────────────────────────────────────────


def _syno_authenticate() -> bool:
    """Authenticate with Synology DSM and store the fresh SID.

    Thread-safe.  The SID from a previous run is always expired after a
    container restart — this must be called on startup before any API call.
    Returns True on success.
    """
    global _syno_sid

    params: dict = {
        "api": "SYNO.API.Auth",
        "version": "7",
        "method": "login",
        "account": SYNOLOGY_LOGIN,
        "passwd": SYNOLOGY_PASSWORD,
        "session": "SurveillanceStation",
        "format": "cookie",
    }
    if SYNOLOGY_OTP:
        params["otp_code"] = SYNOLOGY_OTP
        log.info("Synology auth: using OTP")

    try:
        resp = requests.get(SYNOLOGY_URL, params=params, timeout=API_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            sid = body["data"]["sid"]
            with _syno_sid_lock:
                _syno_sid = sid
            log.info(f"Synology: authenticated (SID prefix: {sid[:10]}…)")
            return True

        code = body.get("error", {}).get("code", "?")
        log.error(f"Synology auth failed: error code {code}")
        if SYNOLOGY_OTP:
            log.error(
                "OTP-based re-authentication failed — the TOTP code may have "
                "rotated. Restart the container with a fresh SYNO_OTP value."
            )
        return False

    except requests.RequestException as exc:
        log.error(f"Synology auth request failed: {exc}")
        return False
    except (KeyError, ValueError) as exc:
        log.error(f"Synology auth response parse error: {exc}")
        return False


def _syno_api_get(params: dict, _retry: bool = True) -> Optional[dict]:
    """Make a Synology JSON API GET request, injecting the current SID.

    On session-expiry errors (codes 105, 106, 119), re-authenticates once
    and retries automatically.  Concurrent re-auth attempts are serialised
    by _syno_reauth_lock so only one thread re-auths; others reuse the
    fresh SID.
    Returns the parsed response dict, or None on network / parse error.
    """
    with _syno_sid_lock:
        current_sid = _syno_sid

    if current_sid is None:
        log.error("_syno_api_get: no active session")
        return None

    try:
        resp = requests.get(
            SYNOLOGY_URL,
            params={**params, "_sid": current_sid},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()

        if not body.get("success") and _retry:
            code = body.get("error", {}).get("code", 0)
            # 105 = session expired / insufficient privilege
            # 106 = connection timed out (session gone)
            # 119 = SID not found
            if code in (105, 106, 119):
                log.warning(
                    f"Synology session invalid (code={code}), re-authenticating…"
                )
                with _syno_reauth_lock:
                    # Another thread may have already refreshed the SID while
                    # we waited on the lock — reuse it if so.
                    with _syno_sid_lock:
                        new_sid = _syno_sid
                    if new_sid != current_sid:
                        log.debug("SID was refreshed by another thread — retrying")
                    elif not _syno_authenticate():
                        return body
                return _syno_api_get(params, _retry=False)

        return body

    except requests.Timeout:
        log.error(f"Synology API timeout ({API_TIMEOUT}s)")
        return None
    except requests.RequestException as exc:
        log.error(f"Synology API request error: {exc}")
        return None
    except ValueError as exc:
        log.error(f"Synology API response parse error: {exc}")
        return None


# ─── Camera configuration ─────────────────────────────────────────────────────


def _fetch_cameras() -> None:
    """Fetch the camera list from Synology, update _cam_load, persist to disk.

    Exits the process if cameras cannot be fetched — the service has no
    purpose without a valid camera list.
    """
    log.info("Fetching camera configuration from Synology…")
    body = _syno_api_get({
        "api": "SYNO.SurveillanceStation.Camera",
        "version": "9",
        "method": "List",
    })

    if not body or not body.get("success"):
        code = (body or {}).get("error", {}).get("code", "?")
        log.error(f"Camera list fetch failed (error code: {code})")
        sys.exit(1)

    cameras = body.get("data", {}).get("cameras", [])
    if not cameras:
        log.error("No cameras found in Surveillance Station")
        sys.exit(1)

    cam_data: dict = {}
    summary = ""
    for cam in cameras:
        cid = str(cam["id"])
        cam_data[cid] = {
            "CamId": cid,
            "IP": cam.get("ip", "N/A"),
            "SynoName": cam.get("newName", "Unknown"),
            "Model": cam.get("model", "N/A"),
            "Vendor": cam.get("vendor", "N/A"),
        }
        summary += f"• [{cid}] {cam.get('newName', 'Unknown')}  {cam.get('ip', '')}\n"

    with _cam_load_lock:
        global _cam_load
        _cam_load = cam_data

    # Atomic write: tmp → final so readers never see a partial file.
    try:
        cfg = pathlib.Path(CONFIG_FILE)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.with_suffix(".tmp")
        tmp.write_text(json.dumps(cam_data, indent=2, ensure_ascii=False))
        tmp.replace(cfg)
        log.info(f"Camera config saved → {CONFIG_FILE}")
    except OSError as exc:
        log.error(f"Could not persist camera config (non-fatal): {exc}")

    _send_message(f"✅ Cameras loaded ({len(cam_data)}):\n{summary}")


def _init_cameras() -> None:
    """Load camera config from disk if valid; otherwise fetch from Synology.
    Initialises _cam_state tracking entries for every camera.
    """
    global _cam_load

    cfg_path = pathlib.Path(CONFIG_FILE)
    loaded = False

    if cfg_path.is_file() and cfg_path.stat().st_size > 0:
        try:
            raw = json.loads(cfg_path.read_text())
            # Keep only entries that look like camera records.
            # Legacy configs stored SynologyAuthSid at the top level — skip it.
            cam_data = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and "CamId" in v
            }
            if cam_data:
                with _cam_load_lock:
                    _cam_load = cam_data
                loaded = True
                log.info(
                    f"Camera config loaded from disk: {len(_cam_load)} camera(s)"
                )
        except (OSError, ValueError) as exc:
            log.warning(f"Could not read camera config from disk: {exc}")

    if not loaded:
        log.info("No valid camera config on disk — fetching from Synology…")
        _fetch_cameras()

    with _cam_load_lock:
        cam_ids = list(_cam_load.keys())

    with _cam_state_lock:
        _cam_state.clear()
        for cam_id in cam_ids:
            _cam_state[cam_id] = {
                "last_video_id": None,   # last seen recording ID (None = no event yet)
                "video_offset": 0,       # next download offset in milliseconds
            }

    log.info(f"Tracking {len(_cam_state)} camera(s): {cam_ids}")


# ─── Module-level initialisation (runs when Gunicorn imports the module) ──────

if not _syno_authenticate():
    log.error("Startup authentication with Synology failed. Exiting.")
    sys.exit(1)

_init_cameras()

# ─── Synology video functions ────────────────────────────────────────────────


def _get_latest_recording_id(cam_id: str) -> Optional[str]:
    """Return the ID of the most recent recording for *cam_id*, or None."""
    body = _syno_api_get({
        "api": "SYNO.SurveillanceStation.Recording",
        "version": "6",
        "method": "List",
        "cameraIds": cam_id,
        "offset": "0",
        "limit": "1",
        "fromTime": "0",
        "toTime": "0",
    })

    if not body or not body.get("success"):
        log.error(f"Recording list failed for camera {cam_id}: {body}")
        return None

    try:
        recs = body["data"]["recordings"]
        if not recs:
            log.warning(f"No recordings found for camera {cam_id}")
            return None
        return str(recs[0]["id"])
    except (KeyError, IndexError) as exc:
        log.error(f"Parse error in recording list (camera {cam_id}): {exc}")
        return None


def _download_video(
    video_id: str,
    offset_ms: int,
    dest_path: str,
    _retry: bool = True,
) -> bool:
    """Download one video segment from Synology and write it to *dest_path*.

    Uses streaming I/O (iter_content) to avoid loading the full file into
    memory.  Writes to a .tmp file first and renames atomically on success
    so the consumer never reads a partial download.

    On session-expiry JSON responses, re-authenticates once and retries.
    Concurrent re-auth attempts are serialised by _syno_reauth_lock.
    Returns True on success.
    """
    with _syno_sid_lock:
        current_sid = _syno_sid

    if current_sid is None:
        log.error("_download_video: no active session")
        return False

    params = {
        "api": "SYNO.SurveillanceStation.Recording",
        "version": "6",
        "method": "Download",
        "id": video_id,
        "mountId": "0",
        "offsetTimeMs": str(offset_ms),
        "playTimeMs": str(VIDEO_SEGMENT_DURATION),
        "_sid": current_sid,
    }

    tmp_path = dest_path + ".tmp"

    try:
        resp = requests.get(
            SYNOLOGY_URL,
            params=params,
            stream=True,
            allow_redirects=True,
            timeout=VIDEO_DOWNLOAD_TIMEOUT,
        )
        resp.raise_for_status()

        # Synology returns JSON (not video) when the session is invalid.
        ct = resp.headers.get("Content-Type", "")
        if "application/json" in ct:
            try:
                err = resp.json()
            except ValueError:
                err = {}
            code = err.get("error", {}).get("code", 0)
            if code in (105, 106, 119) and _retry:
                log.warning(
                    f"Session expired during video download (code={code}), "
                    f"re-authenticating…"
                )
                with _syno_reauth_lock:
                    with _syno_sid_lock:
                        new_sid = _syno_sid
                    if new_sid != current_sid:
                        log.debug("SID was refreshed by another thread — retrying")
                    elif not _syno_authenticate():
                        log.error(f"Synology returned JSON error during download: {err}")
                        return False
                return _download_video(video_id, offset_ms, dest_path, _retry=False)
            log.error(f"Synology returned JSON error during download: {err}")
            return False

        # Stream to .tmp, then rename — readers never see a partial file.
        written = 0
        try:
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)
        except requests.RequestException as exc:
            # Synology sometimes closes the connection early at the live edge.
            # A partial file > 100 KB is usually still a playable segment.
            if written > 102_400:
                log.warning(
                    f"Partial download {written / 1024:.0f} KB "
                    f"(rec={video_id} offset={offset_ms}ms) — using partial file"
                )
            else:
                log.error(
                    f"Incomplete download ({written} B) for "
                    f"rec={video_id} offset={offset_ms}ms: {exc}"
                )
                _safe_remove(tmp_path)
                return False

        if written == 0:
            log.warning(
                f"Empty response for rec={video_id} offset={offset_ms}ms "
                f"(live edge or end of recording)"
            )
            _safe_remove(tmp_path)
            return False

        os.replace(tmp_path, dest_path)
        log.debug(
            f"Video saved: {dest_path}  "
            f"({written / 1024:.1f} KB, offset={offset_ms}ms)"
        )
        return True

    except requests.RequestException as exc:
        log.error(f"Video download network error: {exc}")
        _safe_remove(tmp_path)
        return False
    except OSError as exc:
        log.error(f"Video file write error ({dest_path}): {exc}")
        _safe_remove(tmp_path)
        return False


# ─── Motion processing ────────────────────────────────────────────────────────


def _process_motion(cam_id: str) -> None:
    """Handle one motion event for *cam_id*.  Runs in a daemon thread.

    Acquires the per-camera lock non-blockingly: if the camera is already
    being processed (slow download or Telegram send in progress), the new
    event is dropped rather than queued — Synology will send another webhook
    if motion continues.
    """
    lock = _get_cam_lock(cam_id)
    if not lock.acquire(blocking=False):
        log.info(
            f"Camera {cam_id} already processing — duplicate webhook dropped"
        )
        return

    try:
        with _cam_load_lock:
            cam_name = _cam_load.get(cam_id, {}).get("SynoName", f"Camera {cam_id}")
        video_path = _cam_video_path(cam_id)

        log.info(
            f"Motion: camera {cam_id} ({cam_name}) "
            f"at {time.strftime('%d.%m.%Y %H:%M:%S')}"
        )

        # Give Synology time to finish writing the recording before we fetch it.
        time.sleep(WEBHOOK_TIMEOUT)

        rec_id = _get_latest_recording_id(cam_id)
        if rec_id is None:
            log.error(f"Cannot get recording ID for camera {cam_id}")
            return

        with _cam_state_lock:
            if cam_id not in _cam_state:
                log.error(
                    f"Camera {cam_id} not found in state tracking — "
                    f"possible config reload race; skipping"
                )
                return
            state = _cam_state[cam_id]
            is_new = rec_id != state["last_video_id"]
            if is_new:
                state["last_video_id"] = rec_id
                state["video_offset"] = 0
            else:
                state["video_offset"] += VIDEO_SEGMENT_DURATION
            offset = state["video_offset"]

        if is_new:
            _send_message(f"🔴 Motion detected: {cam_name}")

        if _download_video(rec_id, offset, video_path):
            _send_video(video_path, cam_id)
            _safe_remove(video_path)
        else:
            log.error(
                f"Video download failed: camera {cam_id}, "
                f"rec={rec_id}, offset={offset}ms"
            )

    except Exception as exc:
        log.error(
            f"Unexpected error processing motion for camera {cam_id}: {exc}",
            exc_info=True,
        )
    finally:
        lock.release()


# ─── Flask application ────────────────────────────────────────────────────────

app = Flask(__name__)


def _verify_secret(req) -> bool:
    """Return True if the shared secret is valid (or not configured).

    Checks the X-Webhook-Token request header first, then the ?token= query
    parameter as a fallback for Synology versions that cannot set headers.
    """
    global _secret_warned

    if not WEBHOOK_SECRET:
        if not _secret_warned:
            log.warning(
                "WEBHOOK_SECRET is not configured — the /webhookcam endpoint "
                "is open to anyone who can reach this host. "
                "Set WEBHOOK_SECRET to a random token for security."
            )
            _secret_warned = True
        return True

    provided = (
        req.headers.get("X-Webhook-Token")
        or req.args.get("token", "")
    )
    return provided == WEBHOOK_SECRET


@app.route("/webhookcam", methods=["POST"])
def webhookcam():
    """Receive a motion-detection webhook from Synology Surveillance Station.

    Expected JSON body:
        {"idcam": "1"}

    The handler validates the request and spawns a daemon thread for the
    actual processing, then returns 202 Accepted immediately.  This prevents
    Gunicorn from blocking while waiting for the 5-second pre-recording delay,
    the video download, and the Telegram send.
    """
    if not _verify_secret(flask_request):
        log.warning(
            f"Webhook rejected: bad or missing secret "
            f"(from {flask_request.remote_addr})"
        )
        return "Unauthorized", 401

    # force=True: accept JSON even without Content-Type: application/json
    # silent=True: return None on parse error instead of raising 400
    body = flask_request.get_json(force=True, silent=True)
    if body is None:
        raw = flask_request.get_data(as_text=True)
        log.warning(
            f"Webhook: unparseable body from {flask_request.remote_addr}: "
            f"{raw[:200]!r}"
        )
        return "Bad Request: invalid JSON", 400

    if "idcam" not in body:
        log.warning("Webhook rejected: missing 'idcam' field")
        return "Bad Request: missing idcam", 400

    cam_id = str(body["idcam"])

    # Sanitise cam_id: must be a short numeric string (Synology camera IDs).
    if not cam_id.isdigit() or len(cam_id) > 10:
        log.warning(f"Webhook rejected: invalid cam_id {cam_id!r}")
        return "Bad Request: invalid idcam", 400

    with _cam_load_lock:
        known = cam_id in _cam_load
    if not known:
        log.warning(f"Webhook rejected: unknown camera {cam_id!r}")
        return f"Bad Request: unknown camera {cam_id}", 400

    thread = threading.Thread(
        target=_process_motion,
        args=(cam_id,),
        name=f"cam{cam_id}-{int(time.time())}",
        daemon=True,
    )
    thread.start()

    log.info(f"Webhook accepted for camera {cam_id} — processing in background")
    return "Accepted", 202


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint — no authentication required."""
    with _syno_sid_lock:
        authenticated = _syno_sid is not None
    with _cam_load_lock:
        cam_count = len(_cam_load)
    return {
        "status": "ok",
        "cameras": cam_count,
        "authenticated": authenticated,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, 200


# ─── Development entry point ─────────────────────────────────────────────────
# In production Gunicorn is used; this block is only for local dev runs.

if __name__ == "__main__":
    log.info("Development mode — use Gunicorn for production")
    log.info("Webhook:      http://localhost:7878/webhookcam")
    log.info("Health check: http://localhost:7878/health")
    app.run(host="0.0.0.0", port=7878, debug=False)
