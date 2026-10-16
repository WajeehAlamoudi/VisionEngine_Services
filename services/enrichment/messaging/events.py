"""event.v1: one batch of enriched detections per processed source frame."""

import json
import math

from .detections import validate as validate_detections

MAX_BATCH_BYTES = 8 * 1024 * 1024


def validate(batch):
    if not isinstance(batch, dict) or batch.get("message_type") != "event.v1":
        raise ValueError("expected event.v1")
    events = batch.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    validate_detections(
        {**batch, "message_type": "detections.v1", "detections": events}
    )
    for event in events:
        for key in ("camera_id", "model_id"):
            if event.get(key) != batch[key]:
                raise ValueError("event identity differs from its batch")
        if (
            event.get("frame_w") != batch["width"]
            or event.get("frame_h") != batch["height"]
        ):
            raise ValueError("event frame dimensions differ from its batch")
        for key in ("camera_name", "capture_ts"):
            if not isinstance(event.get(key), str) or not event[key]:
                raise ValueError(f"invalid {key}")
        for key, maximum in (
            ("bbox_w", batch["width"]),
            ("bbox_h", batch["height"]),
            ("center_x", batch["width"]),
            ("center_y", batch["height"]),
            ("anchor_x", batch["width"]),
            ("anchor_y", batch["height"]),
            ("anchor_x_norm", 1),
            ("anchor_y_norm", 1),
        ):
            value = event.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= maximum
            ):
                raise ValueError(f"invalid {key}")
        if not isinstance(event.get("attributes"), dict):
            raise ValueError("attributes must be a mapping")
        if "zone" in event:
            raise ValueError("zone assignment belongs to rules")
    metadata = batch.get("enrichment", {})
    elapsed = metadata.get("enrichment_ms") if isinstance(metadata, dict) else None
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("invalid enrichment time")
    if (
        type(metadata.get("enriched_at_ns")) is not int
        or metadata["enriched_at_ns"] < 1
    ):
        raise ValueError("invalid enrichment completion timestamp")


def encode(batch):
    validate(batch)
    payload = json.dumps(batch, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("event batch is too large")
    return [f"event.v1/{batch['camera_id']}".encode(), payload]


def decode(parts):
    if len(parts) != 2 or len(parts[1]) > MAX_BATCH_BYTES:
        raise ValueError("expected topic and bounded JSON event batch")
    batch = json.loads(parts[1])
    validate(batch)
    if parts[0] != f"event.v1/{batch['camera_id']}".encode():
        raise ValueError("event topic differs from camera ID")
    return batch
