"""rows.v1 batches carry original {table, row} items and source correlation."""

import json
import math

MAX_BATCH_BYTES = 16 * 1024 * 1024


def validate(batch):
    if not isinstance(batch, dict) or batch.get("message_type") != "rows.v1":
        raise ValueError("expected rows.v1")
    is_health = batch.get("source_type") == "health.v1"
    identity_fields = (
        ("device_id", "stream_id")
        if is_health
        else ("camera_id", "stream_id", "model_id")
    )
    for key in identity_fields:
        if not isinstance(batch.get(key), str) or not batch[key]:
            raise ValueError(f"invalid {key}")
    for key in ("sequence", "received_at_ns"):
        if type(batch.get(key)) is not int or batch[key] < 1:
            raise ValueError(f"invalid {key}")
    if batch.get("source_type") not in ("event.v1", "alert.v1", "health.v1"):
        raise ValueError("invalid rows source type")
    if not isinstance(batch.get("rows"), list):
        raise ValueError("rows must be a list")
    for item in batch["rows"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"table", "row"}
            or not isinstance(item["table"], str)
            or not item["table"]
            or not isinstance(item["row"], dict)
        ):
            raise ValueError("expected table/row envelope")
    metadata = batch.get("row_builder")
    if not isinstance(metadata, dict):
        raise ValueError("missing row builder metadata")
    for key in (
        "input_count",
        "detection_rows",
        "notification_rows",
        "skipped_no_table",
    ):
        if type(metadata.get(key)) is not int or metadata[key] < 0:
            raise ValueError(f"invalid {key}")
    if (
        type(metadata.get("node_rows", 0)) is not int
        or metadata.get("node_rows", 0) < 0
        or type(metadata.get("skipped_disabled", 0)) is not int
        or metadata.get("skipped_disabled", 0) < 0
    ):
        raise ValueError("invalid node/disabled counts")
    if (
        metadata["detection_rows"]
        + metadata["notification_rows"]
        + metadata.get("node_rows", 0)
        != len(batch["rows"])
        or metadata["input_count"]
        != len(batch["rows"])
        + metadata["skipped_no_table"]
        + metadata.get("skipped_disabled", 0)
        or (batch["source_type"] == "event.v1" and metadata["notification_rows"])
        or (batch["source_type"] == "alert.v1" and metadata["detection_rows"])
        or (is_health and (metadata["detection_rows"] or metadata["notification_rows"]))
        or (not is_health and metadata.get("node_rows", 0))
    ):
        raise ValueError("row counts differ from payload")
    elapsed = metadata.get("build_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("invalid build time")
    if type(metadata.get("built_at_ns")) is not int or metadata["built_at_ns"] < 1:
        raise ValueError("invalid build timestamp")


def topic(batch):
    if batch.get("source_type") == "health.v1":
        return f"rows.v1/device/{batch['device_id']}".encode()
    return f"rows.v1/{batch['camera_id']}".encode()


def encode(batch):
    validate(batch)
    payload = json.dumps(batch, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("rows batch is too large")
    return [topic(batch), payload]


def decode(parts):
    if len(parts) != 2 or len(parts[1]) > MAX_BATCH_BYTES:
        raise ValueError("expected topic and bounded row JSON")
    batch = json.loads(parts[1])
    validate(batch)
    if parts[0] != topic(batch):
        raise ValueError("row topic differs from camera ID")
    # Reject non-finite values in arbitrary flattened attributes too.
    json.dumps(batch, allow_nan=False)
    return batch
