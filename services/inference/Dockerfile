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
ENV INFERENCE_HEALTH_FILE=/tmp/inference-health.json
ENV YOLO_CONFIG_DIR=/tmp
ENV YOLO_AUTOINSTALL=False

# Application working directory.
WORKDIR /app

# OpenCV runtime libraries and Python dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY services/inference/requirements.txt /app/services/inference/requirements.txt
RUN if [ "${INSTALL_TORCH}" = "1" ]; then python -m pip install --no-cache-dir torch torchvision --index-url ${TORCH_INDEX_URL}; fi

# Keep model runtime installation separate from service dependencies for retries.
RUN python -m pip install --no-cache-dir --retries 5 --timeout 120 -r services/inference/requirements.txt

# Self-contained service code and the configuration parser.
COPY services/inference/ /app/services/inference/
COPY config/controller/ /app/config/controller/

# Detections and optional annotated-frame output ports.
EXPOSE 5556 5557
STOPSIGNAL SIGTERM

# Health reports completed inference, not only a running process.
HEALTHCHECK --interval=10s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "services.inference.runtime.healthcheck", "--file", "/tmp/inference-health.json"]

# Compose supplies the model ID and camera endpoint arguments.
ENTRYPOINT ["python", "-m", "services.inference"]
