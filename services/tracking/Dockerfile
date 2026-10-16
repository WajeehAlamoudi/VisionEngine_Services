# Build from the independent VisionEngine_Services project root.

# Runtime: override the base and Torch index for a compatible GPU/vendor build.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG INSTALL_TORCH=1

# Python logging and bytecode behavior.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Runtime state; model assets and configuration remain read-only mounts.
ENV TRACKING_HEALTH_FILE=/tmp/tracking-health.json

# Application working directory.
WORKDIR /app

# OpenCV runtime libraries and Python dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY services/tracking/requirements.txt /app/services/tracking/requirements.txt
RUN if [ "${INSTALL_TORCH}" = "1" ]; then python -m pip install --no-cache-dir torch torchvision --index-url ${TORCH_INDEX_URL}; fi

# Keep model runtime installation separate from service dependencies for retries.
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/tracking/requirements.txt

# Self-contained service code and the configuration parser.
COPY services/tracking/ /app/services/tracking/
COPY config/controller/ /app/config/controller/

# Tracked detection output port.
EXPOSE 5558
STOPSIGNAL SIGTERM

# Health reports completed tracking, not only a running process.
HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "services.tracking.runtime.healthcheck", "--file", "/tmp/tracking-health.json"]

# Compose supplies the camera ID and selected input endpoint arguments.
ENTRYPOINT ["python", "-m", "services.tracking"]
