"""Detection geometry and context; zone evaluation belongs to the rules service."""

from copy import deepcopy
from datetime import datetime, timezone
import time


def enrich(batch, camera):
    started = time.perf_counter()
    if batch["camera_id"] != camera.id or batch["model_id"] != camera.model_id:
        raise ValueError("batch does not match this camera/model")
    width, height = batch["width"], batch["height"]
    seconds, nanos = divmod(batch["received_at_ns"], 1_000_000_000)
    capture_ts = (
        datetime.fromtimestamp(seconds, timezone.utc)
        .replace(microsecond=nanos // 1000)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    events = []
    for detection in batch["detections"]:
        x1, y1, x2, y2 = detection["bbox"]
        center_x, center_y = (x1 + x2) / 2, (y1 + y2) / 2
        # Preserve the original Edge convention, including the clipped-feet fix.
        anchor_y = (
            center_y if detection["class_name"] != "person" or y2 >= height - 1 else y2
        )
        event = {
            "track_id": detection.get("track_id"),
            "class_name": detection["class_name"],
            "confidence": detection["confidence"],
            "bbox": list(detection["bbox"]),
            "bbox_w": x2 - x1,
            "bbox_h": y2 - y1,
            "center_x": center_x,
            "center_y": center_y,
            "anchor_x": round(center_x, 1),
            "anchor_y": round(anchor_y, 1),
            "anchor_x_norm": round(center_x / width, 4),
            "anchor_y_norm": round(anchor_y / height, 4),
            "camera_id": camera.id,
            "camera_name": camera.name,
            "model_id": batch["model_id"],
            "frame_w": width,
            "frame_h": height,
            "capture_ts": capture_ts,
            "attributes": deepcopy(detection.get("attributes", {})),
        }
        events.append(event)
    result = {
        key: value
        for key, value in batch.items()
        if key not in ("message_type", "detections")
    }
    return {
        **result,
        "message_type": "event.v1",
        "events": events,
        "enrichment": {
            "enriched_at_ns": time.time_ns(),
            "enrichment_ms": (time.perf_counter() - started) * 1000,
        },
    }
