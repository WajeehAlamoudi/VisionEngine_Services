# Build from the independent VisionEngine_Services project root.

# Lightweight metadata collector; no model or GPU dependencies.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python output and bytecode behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Monitor's own heartbeat is separate from watched service snapshots.
ENV MONITOR_HEALTH_FILE=/tmp/monitor-health.json

# Application working directory.
WORKDIR /app

# Cache dependency installation before application source.
COPY services/monitor/requirements.txt /app/services/monitor/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/monitor/requirements.txt

# Standalone service code and independent configuration parser.
COPY services/monitor/ /app/services/monitor/
COPY config/controller/ /app/config/controller/

# Metadata publisher; Compose controls host publishing.
EXPOSE 5563
STOPSIGNAL SIGTERM

# Collector liveness is independent of the device health being reported.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.monitor.runtime.healthcheck", "--file", "/tmp/monitor-health.json"]

# Compose appends watched snapshots, camera assignments and configuration.
ENTRYPOINT ["python", "-m", "services.monitor"]
