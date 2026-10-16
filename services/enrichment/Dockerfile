# Build from the independent VisionEngine_Services project root.

# Lightweight CPU runtime; enrichment uses no model, Torch or OpenCV.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python output and bytecode behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime health snapshot; original configuration remains a read-only mount.
ENV ENRICHMENT_HEALTH_FILE=/tmp/enrichment-health.json

# Application working directory.
WORKDIR /app

# Install transport and configuration dependencies before copying application code.
COPY services/enrichment/requirements.txt /app/services/enrichment/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/enrichment/requirements.txt

# Self-contained service and the independent project's configuration parser.
COPY services/enrichment/ /app/services/enrichment/
COPY config/controller/ /app/config/controller/

# Enriched event output; Compose controls host publishing.
EXPOSE 5559
STOPSIGNAL SIGTERM

# Health checks completed processing, including empty batches.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.enrichment.runtime.healthcheck", "--file", "/tmp/enrichment-health.json"]

# Compose appends camera ID, configuration and endpoint arguments.
ENTRYPOINT ["python", "-m", "services.enrichment"]
