"""Build the original Edge database row fields and table envelopes."""

import time

from ..messaging.health import validate as validate_health


def node_row(health):
    """Exact field mapping from the original core.health.rows._heartbeat_row."""
    return {
        "device_id": health["device_id"],
        "name": health["name"],
        "location": health["location"],
        "status": health["status"],
        "cameras_active": health["cameras_active"],
        "cameras_error": health["cameras_error"],
        "detections_total": sum(c["detections_total"] for c in health["cameras"]),
        "buffer_pending": health["buffer"]["pending"],
        "uptime_seconds": health["uptime_seconds"],
        "ts": health["ts"],
    }


def detection_row(event):
    x1, y1, x2, y2 = (int(value) for value in event["bbox"])
    row = {
        "camera_id": event["camera_id"],
        "camera_name": event["camera_name"],
        "model_id": event["model_id"],
        "track_id": event.get("track_id"),
        "class": event["class_name"],
        "confidence": round(event["confidence"], 4),
        "bbox_x1": x1,
        "bbox_y1": y1,
        "bbox_x2": x2,
        "bbox_y2": y2,
        "bbox_w": x2 - x1,
        "bbox_h": y2 - y1,
        "anchor_x": event["anchor_x"],
        "anchor_y": event["anchor_y"],
        "anchor_x_norm": event["anchor_x_norm"],
        "anchor_y_norm": event["anchor_y_norm"],
        "frame_w": event["frame_w"],
        "frame_h": event["frame_h"],
        "zone": event["zone"],
        "ts": event["capture_ts"],
    }
    # Preserve legacy behavior: attributes are flattened last, including collisions.
    row.update(event.get("attributes", {}))
    return row


def notification_row(alert):
    # Legacy notification rows place rule fields before detection fields/attributes.
    return {
        "rule_name": alert["rule_name"],
        "severity": alert["severity"],
        "message": alert["message"],
        **detection_row(alert["detection"]),
    }


def route_table(camera, class_name):
    if camera.raw_table:
        return camera.raw_table
    for entry in camera.routing:
        if not entry.classes or class_name in entry.classes:
            return entry.raw_table
    return None


class RowBuilder:
    def __init__(self, cameras, models, rules, device=None):
        self.device = device
        self.cameras = cameras
        self.models = models
        self.rules = {rule.name: rule for rule in rules if rule.enabled}

    def validate_input(self, batch):
        if batch.get("message_type") == "health.v1":
            validate_health(batch)
            if self.device is None or batch["device_id"] != self.device.id:
                raise ValueError("monitor device differs from configured device")
            for key in ("name", "location"):
                if batch["health"][key] != getattr(self.device, key):
                    raise ValueError(
                        "monitor device identity differs from configuration"
                    )
            return
        camera = self.cameras.get(batch["camera_id"])
        if camera is None or batch["model_id"] != camera.model_id:
            raise ValueError("batch camera is disabled/unknown or model differs")
        is_alert = batch["message_type"] == "alert.v1"
        items = batch["alerts"] if is_alert else batch["events"]
        for item in items:
            event = item["detection"] if is_alert else item
            if event["class_name"] not in camera.classes:
                raise ValueError("event class is outside configured camera classes")
            if is_alert:
                rule = self.rules.get(item["rule_name"])
                if (
                    rule is None
                    or not rule.notify
                    or item.get("notifications_table") != rule.notifications_table
                    or item["severity"] != rule.severity
                    or (rule.cameras and camera.id not in rule.cameras)
                    or (
                        rule.class_name != "*"
                        and event["class_name"] != rule.class_name
                    )
                    or (rule.zones and event["zone"] not in rule.zones)
                ):
                    raise ValueError("alert differs from configured notification rule")
        # Scheduling/cooldown already happened in rules; never evaluate them again.

    def build(self, batch):
        self.validate_input(batch)
        started = time.perf_counter()
        if batch["message_type"] == "health.v1":
            enabled = self.device.heartbeat.enabled
            return {
                "message_type": "rows.v1",
                "source_type": "health.v1",
                "device_id": batch["device_id"],
                "stream_id": batch["stream_id"],
                "sequence": batch["sequence"],
                "received_at_ns": batch["received_at_ns"],
                "rows": [
                    {
                        "table": self.device.heartbeat.table,
                        "row": node_row(batch["health"]),
                    }
                ]
                if enabled
                else [],
                "row_builder": {
                    "built_at_ns": time.time_ns(),
                    "build_ms": (time.perf_counter() - started) * 1000,
                    "input_count": 1,
                    "detection_rows": 0,
                    "notification_rows": 0,
                    "node_rows": int(enabled),
                    "skipped_no_table": 0,
                    "skipped_disabled": int(not enabled),
                },
            }
        camera = self.cameras[batch["camera_id"]]
        is_alert = batch["message_type"] == "alert.v1"
        rows, skipped = [], 0
        items = batch["alerts"] if is_alert else batch["events"]
        for item in items:
            table = (
                self.rules[item["rule_name"]].notifications_table
                if is_alert
                else route_table(camera, item["class_name"])
            )
            if table:
                rows.append(
                    {
                        "table": table,
                        "row": notification_row(item)
                        if is_alert
                        else detection_row(item),
                    }
                )
            else:
                skipped += 1
        context = {
            key: value
            for key, value in batch.items()
            if key not in ("message_type", "events", "alerts")
        }
        return {
            **context,
            "message_type": "rows.v1",
            "source_type": batch["message_type"],
            "rows": rows,
            "row_builder": {
                "built_at_ns": time.time_ns(),
                "build_ms": (time.perf_counter() - started) * 1000,
                "input_count": len(items),
                "detection_rows": 0 if is_alert else len(rows),
                "notification_rows": len(rows) if is_alert else 0,
                "node_rows": 0,
                "skipped_disabled": 0,
                "skipped_no_table": skipped,
            },
        }
