# Build from the independent VisionEngine_Services project root.

# Lightweight HTTP delivery worker; no model or database engine is installed.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python console behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime heartbeat; Compose can assign a shared monitor snapshot path.
ENV INGEST_HEALTH_FILE=/tmp/ingest-health.json

# Service code runs from the independent project root.
WORKDIR /app

# Cache dependency installation independently from source changes.
COPY services/ingest/requirements.txt /app/services/ingest/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/ingest/requirements.txt

# Service-local delivery, transport and configuration parser.
COPY services/ingest/ /app/services/ingest/
COPY config/controller/ /app/config/controller/

# Observational status publisher; backend delivery uses outbound HTTP.
EXPOSE 5566
STOPSIGNAL SIGTERM

# Worker liveness remains healthy during backend outages and retries.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.ingest.runtime.healthcheck", "--file", "/tmp/ingest-health.json"]

# Compose supplies configuration and endpoint arguments.
ENTRYPOINT ["python", "-m", "services.ingest"]
