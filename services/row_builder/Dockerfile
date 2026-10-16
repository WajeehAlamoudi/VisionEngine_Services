# Build from the independent VisionEngine_Services project root.

# Lightweight runtime: row building needs no model, Torch or OpenCV.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python console behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Temporary runtime health snapshot.
ENV ROW_BUILDER_HEALTH_FILE=/tmp/row-builder-health.json

# Application working directory.
WORKDIR /app

# Cache dependency installation separately from source changes.
COPY services/row_builder/requirements.txt /app/services/row_builder/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/row_builder/requirements.txt

# Service-local messaging/processing and independent configuration parsers.
COPY services/row_builder/ /app/services/row_builder/
COPY config/controller/ /app/config/controller/

# Row output; host publishing is configured by Compose.
EXPOSE 5562
STOPSIGNAL SIGTERM

# Completed processing and supervisor heartbeat.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.row_builder.runtime.healthcheck", "--file", "/tmp/row-builder-health.json"]

# Compose provides configuration and rules endpoints.
ENTRYPOINT ["python", "-m", "services.row_builder"]
