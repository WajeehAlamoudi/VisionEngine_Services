# Build from the independent VisionEngine_Services project root.

# Python includes SQLite; no separate database server is required.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# Python console behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime heartbeat; Compose can assign a shared monitor snapshot path.
ENV BUFFER_HEALTH_FILE=/tmp/buffer-health.json

# Relative api.buffer.path is resolved from this working directory.
WORKDIR /app

# Cache dependency installation independently from source changes.
COPY services/buffer/requirements.txt /app/services/buffer/requirements.txt
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/buffer/requirements.txt

# Service-local storage, transport and configuration parser.
COPY services/buffer/ /app/services/buffer/
COPY config/controller/ /app/config/controller/

# Acknowledged RPC and observational status publisher.
EXPOSE 5564 5565
STOPSIGNAL SIGTERM

# Serving-loop heartbeat is valid even with zero pending rows.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "services.buffer.runtime.healthcheck", "--file", "/tmp/buffer-health.json"]

# Compose supplies configuration and endpoint arguments.
ENTRYPOINT ["python", "-m", "services.buffer"]
