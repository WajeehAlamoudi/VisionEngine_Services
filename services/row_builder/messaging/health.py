"""health.v1 transports monitor measurements, never database rows."""

from datetime import datetime
import json
import math

MAX_BATCH_BYTES = 1024 * 1024


def validate(batch):
    if not isinstance(batch, dict) or batch.get("message_type") != "health.v1":
        raise ValueError("expected health.v1")
    for key in ("device_id", "stream_id"):
        if not isinstance(batch.get(key), str) or not batch[key]:
            raise ValueError(f"invalid {key}")
    for key in ("sequence", "received_at_ns"):
        if type(batch.get(key)) is not int or batch[key] < 1:
            raise ValueError(f"invalid {key}")
    health = batch.get("health")
    if not isinstance(health, dict) or health.get("device_id") != batch["device_id"]:
        raise ValueError("health identity differs from envelope")
    for key in ("name", "location", "ts"):
        if not isinstance(health.get(key), str) or not health[key]:
            raise ValueError(f"invalid health {key}")
    timestamp = datetime.fromisoformat(health["ts"].replace("Z", "+00:00"))
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("health timestamp must be UTC")
    if health.get("status") not in ("ok", "degraded", "error"):
        raise ValueError("invalid device status")
    for key in ("cameras_active", "cameras_error", "uptime_seconds"):
        value = health.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid health {key}")
        if key != "uptime_seconds" and type(value) is not int:
            raise ValueError("camera counts must be integers")
    cameras = health.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError("health cameras must be a list")
    for camera in cameras:
        value = camera.get("detections_total") if isinstance(camera, dict) else None
        if type(value) is not int or value < 0:
            raise ValueError("invalid camera detection count")
    buffer = health.get("buffer")
    pending = buffer.get("pending") if isinstance(buffer, dict) else None
    unavailable = pending is None and buffer.get("available") is False
    if not unavailable and (type(pending) is not int or pending < 0):
        raise ValueError("invalid buffer pending count")
    json.dumps(batch, allow_nan=False)


def encode(batch):
    validate(batch)
    payload = json.dumps(batch, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("health batch is too large")
    return [f"health.v1/{batch['device_id']}".encode(), payload]


def decode(parts):
    if len(parts) != 2 or len(parts[1]) > MAX_BATCH_BYTES:
        raise ValueError("expected topic and bounded health JSON")
    batch = json.loads(parts[1])
    validate(batch)
    if parts[0] != f"health.v1/{batch['device_id']}".encode():
        raise ValueError("health topic differs from device ID")
    return batch
