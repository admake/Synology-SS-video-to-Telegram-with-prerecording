# syntax=docker/dockerfile:1

FROM python:alpine

# Create non-root user for security
RUN addgroup -g 1000 appuser && adduser -D -u 1000 -G appuser appuser

# Setup Python virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PIP_NO_CACHE_DIR=off

# Copy requirements first (for better layer caching)
COPY requirements.txt .

# Install dependencies
RUN pip3 install --upgrade pip setuptools-rust wheel && \
    pip3 install -r requirements.txt && \
    rm -rf /root/.cache /root/.cargo

# Create app directory and set permissions
WORKDIR /app
RUN chown -R appuser:appuser /app

# Copy ALL source files — config.py and utils.py are required by main.py
COPY --chown=appuser:appuser src/ /app/

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 7878

# Health check — /health returns 200 for both GET and any method.
# /webhookcam only accepts POST, so the old check always returned 405.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:7878/health || exit 1

# Run with a single sync worker.
# Using >1 workers causes state inconsistency because arr_cam_move and
# syno_sid are in-process globals — each worker would have its own copy.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:7878", \
     "--workers", "1", \
     "--worker-class", "sync", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "main:app"]
