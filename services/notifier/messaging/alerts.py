"""alert.v1: rule matches and destinations; delivery belongs to notifier/storage."""

import json

from .events import validate as validate_events

MAX_BATCH_BYTES = 16 * 1024 * 1024


def validate(batch):
    if (
        not isinstance(batch, dict)
        or batch.get("message_type") != "alert.v1"
        or not isinstance(batch.get("alerts"), list)
    ):
        raise ValueError("expected alert.v1 batch")
    detections = []
    for alert in batch["alerts"]:
        if not isinstance(alert, dict):
            raise ValueError("invalid alert")
        if not isinstance(alert.get("rule_name"), str) or not alert["rule_name"]:
            raise ValueError("invalid rule name")
        if alert.get("severity") not in (
            "info",
            "warning",
            "critical",
        ) or not isinstance(alert.get("message"), str):
            raise ValueError("invalid alert severity/message")
        table = alert.get("notifications_table")
        if table is not None and (not isinstance(table, str) or not table):
            raise ValueError("invalid notification table")
        detections.append(alert.get("detection"))
    validate_events(
        {**batch, "message_type": "event.v1", "events": detections}, ruled=True
    )


def encode(batch):
    validate(batch)
    payload = json.dumps(batch, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError("alert batch is too large")
    return [f"alert.v1/{batch['camera_id']}".encode(), payload]


def decode(parts):
    if len(parts) != 2 or len(parts[1]) > MAX_BATCH_BYTES:
        raise ValueError("expected topic and bounded alert JSON")
    batch = json.loads(parts[1])
    validate(batch)
    if parts[0] != f"alert.v1/{batch['camera_id']}".encode():
        raise ValueError("alert topic differs from camera ID")
    return batch
