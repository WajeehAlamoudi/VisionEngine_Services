# Build from the independent VisionEngine_Services project root.

# Lightweight CPU runtime; rules uses no model, Torch or OpenCV.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python output and bytecode behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime health snapshot; original configuration remains a read-only mount.
ENV RULES_HEALTH_FILE=/tmp/rules-health.json

# Application working directory.
WORKDIR /app

# Install transport and configuration dependencies before copying application code.
COPY services/rules/requirements.txt /app/services/rules/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/rules/requirements.txt

# Self-contained service and the independent project's configuration parser.
COPY services/rules/ /app/services/rules/
COPY config/controller/ /app/config/controller/

# Accepted event and alert outputs; Compose controls host publishing.
EXPOSE 5560 5561
STOPSIGNAL SIGTERM

# Health checks completed processing, including empty batches.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.rules.runtime.healthcheck", "--file", "/tmp/rules-health.json"]

# Compose appends instance ID, configuration and camera input endpoints.
ENTRYPOINT ["python", "-m", "services.rules"]
