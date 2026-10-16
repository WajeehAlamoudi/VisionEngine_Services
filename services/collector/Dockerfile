# Build from the VisionEngine_Services project root.

# Base: Python 3.12 on a minimal Debian Linux image.
FROM python:3.12-slim-bookworm

# Python: flush logs immediately and avoid bytecode cache files.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health: status file written by the collector process inside the container.
ENV COLLECTOR_HEALTH_FILE=/tmp/collector-health.json

# Application working directory inside the container.
WORKDIR /app

# Dependencies: install separately so unchanged requirements can reuse build cache.
COPY services/collector/requirements.txt /app/services/collector/requirements.txt
RUN python -m pip install --no-cache-dir -r services/collector/requirements.txt

# Application code and the configuration parser it uses.
COPY services/collector/ /app/services/collector/
COPY config/controller/ /app/config/controller/

# Dataset-status output port; Compose controls publishing this port to the host.
EXPOSE 5568

# Graceful shutdown signal sent to the running application.
STOPSIGNAL SIGTERM

# Health: check every 10s, with startup grace and three consecutive failures.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "services.collector.runtime.healthcheck", "--file", "/tmp/collector-health.json"]

# Start the collector application; Compose supplies its session ID and other arguments.
ENTRYPOINT ["python", "-m", "services.collector"]
