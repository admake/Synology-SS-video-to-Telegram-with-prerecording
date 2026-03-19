# syntax=docker/dockerfile:1

# ─── Stage 1: build dependencies ─────────────────────────────────────────────
# This stage installs all Python packages into an isolated venv.
# Build tools (gcc, musl-dev) stay here and never reach the final image.
FROM python:3.13-alpine AS builder

# gcc + musl-dev compile C extensions pulled in by cryptography/cffi.
# libffi-dev is required by cffi itself.
RUN apk add --no-cache gcc musl-dev libffi-dev

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt


# ─── Stage 2: lean runtime image ─────────────────────────────────────────────
# Only the pre-built venv is copied — no compiler, no pip cache, no build deps.
FROM python:3.13-alpine AS final

LABEL org.opencontainers.image.title="Synology SS → Telegram Bridge" \
      org.opencontainers.image.description="Webhook bridge: Synology Surveillance Station motion events → Telegram video" \
      org.opencontainers.image.source="https://github.com/admake/Synology-SS-video-to-Telegram-with-prerecording"

# Non-root user — the app never needs to write outside /bot (volume mount).
RUN addgroup -g 1000 appuser \
 && adduser -D -u 1000 -G appuser appuser

# Copy the venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    # Do not write .pyc files — the container is ephemeral.
    PYTHONDONTWRITEBYTECODE=1 \
    # Force unbuffered stdout/stderr so log lines appear immediately.
    PYTHONUNBUFFERED=1 \
    # Print a traceback on fatal signals (SIGSEGV, etc.) — aids debugging.
    PYTHONFAULTHANDLER=1

WORKDIR /app

# Copy application source.  WORKDIR is already set, so paths are correct.
COPY --chown=appuser:appuser src/ /app/

USER appuser

EXPOSE 7878

# Health check uses Python's stdlib — no extra tools required.
# Verifies that the /health endpoint returns HTTP 200 with status=ok.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "\
import urllib.request, json, sys; \
r = urllib.request.urlopen('http://localhost:7878/health', timeout=5); \
sys.exit(0 if json.loads(r.read()).get('status') == 'ok' else 1)"

# gthread worker: OS-thread-per-request so Gunicorn can accept new webhooks
# while background daemon-threads process previous motion events.
# workers=1: in-process state (SID, cam tracking) must not be forked.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:7878", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "main:app"]
