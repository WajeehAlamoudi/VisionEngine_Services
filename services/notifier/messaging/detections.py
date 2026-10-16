"""detections.v1: one result batch per processed frame, including empty results."""

import json
import math

MAX_BATCH_BYTES = 4 * 1024 * 1024


def validate(batch):
    if not isinstance(batch, dict) or batch.get("message_type") != "detections.v1":
        raise ValueError("expected detections.v1")
    for key in ("camera_id", "stream_id", "model_id", "model_version"):
        if not isinstance(batch.get(key), str) or not batch[key]:
            raise ValueError(f"invalid {key}")
    for key in ("sequence", "received_at_ns", "processed_at_ns", "width", "height"):
        if type(batch.get(key)) is not int or batch[key] < 1:
            raise ValueError(f"invalid {key}")
    elapsed = batch.get("inference_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("invalid inference time")
    if not isinstance(batch.get("detections"), list):
        raise ValueError("detections must be a list")
    for detection in batch["detections"]:
        if not isinstance(detection, dict) or not isinstance(
            detection.get("class_name"), str
        ):
            raise ValueError("invalid detection")
        confidence = detection.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("invalid confidence")
        box = detection.get("bbox")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in box)
        ):
            raise ValueError("invalid xyxy bounding box")
        if (
            not 0 <= box[0] <= box[2] <= batch["width"]
            or not 0 <= box[1] <= box[3] <= batch["height"]
        ):
            raise ValueError("bounding box is outside its frame")
        if detection.get("track_id") is not None and not isinstance(
            detection["track_id"], str
        ):
            raise ValueError("invalid track ID")


def encode(batch):
    validate(batch)
    payload = json.dumps(batch, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("detection batch is too large")
    return [f"detections.v1/{batch['camera_id']}".encode(), payload]


def decode(parts):
    if len(parts) != 2 or len(parts[1]) > MAX_BATCH_BYTES:
        raise ValueError("expected topic and bounded JSON detection batch")
    batch = json.loads(parts[1])
    validate(batch)
    if parts[0] != f"detections.v1/{batch['camera_id']}".encode():
        raise ValueError("detection topic differs from camera ID")
    return batch
