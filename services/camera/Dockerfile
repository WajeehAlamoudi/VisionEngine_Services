# Build from the VisionEngine_Services project root.

# Base: Python 3.12 on a minimal Debian Linux image.
FROM python:3.12-slim-bookworm

# Python: flush logs immediately and avoid bytecode cache files.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health: status file written by the camera process inside the container.
ENV CAMERA_HEALTH_FILE=/tmp/camera-health.json

# Application working directory inside the container.
WORKDIR /app

# Dependencies: install separately so unchanged requirements can reuse build cache.
COPY services/camera/requirements.txt /app/services/camera/requirements.txt
RUN python -m pip install --no-cache-dir -r services/camera/requirements.txt

# Application code and the configuration parser it uses.
COPY services/camera/ /app/services/camera/
COPY config/controller/ /app/config/controller/

# Frame output port; Compose controls publishing this port to the host.
EXPOSE 5555

# Graceful shutdown signal sent to the running application.
STOPSIGNAL SIGTERM

# Health: check every 10s, with startup grace and three consecutive failures.
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=3 \
  CMD ["python", "-m", "services.camera.runtime.healthcheck", "--file", "/tmp/camera-health.json"]

# Start the camera application; Compose supplies its camera ID and other arguments.
ENTRYPOINT ["python", "-m", "services.camera"]
