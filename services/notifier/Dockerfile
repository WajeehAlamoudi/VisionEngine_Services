# Build from the independent VisionEngine_Services project root.

# Lightweight HTTP/SMTP delivery worker; no model or database engine is installed.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python console behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime heartbeat; Compose can assign a shared monitor snapshot path.
ENV NOTIFIER_HEALTH_FILE=/tmp/notifier-health.json

# Service code runs from the independent project root.
WORKDIR /app

# Cache dependency installation independently from source changes.
COPY services/notifier/requirements.txt /app/services/notifier/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/notifier/requirements.txt

# Service-local delivery, transport and configuration parser.
COPY services/notifier/ /app/services/notifier/
COPY config/controller/ /app/config/controller/

# Observational status publisher; notification delivery uses outbound HTTP/SMTP.
EXPOSE 5567
STOPSIGNAL SIGTERM

# Worker liveness remains healthy during channel outages and retries.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.notifier.runtime.healthcheck", "--file", "/tmp/notifier-health.json"]

# Compose supplies configuration and endpoint arguments.
ENTRYPOINT ["python", "-m", "services.notifier"]
