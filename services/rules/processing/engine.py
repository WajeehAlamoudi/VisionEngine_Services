"""Original Edge filter/notification semantics adapted to network event batches."""

from datetime import datetime, timezone
import time

from .zones import assign_zone


def _now_hhmm():
    return datetime.now(timezone.utc).strftime("%H:%M")


def format_message(template, event):
    return (
        template.replace("{class}", event["class_name"])
        .replace("{zone}", event["zone"])
        .replace("{camera}", event["camera_id"])
        .replace("{confidence}", f"{event['confidence']:.2f}")
    )


class RulesEngine:
    def __init__(self, rules):
        # The original application passes enabled_rules to its engine.
        self.rules = [rule for rule in rules if rule.enabled]
        self.last_fired = {}

    def filter_and_tag(self, event):
        """None = rejected; [] = accepted; nonempty list = accepted + alerts."""
        if not self.rules:
            return []  # Internal legacy fallback; the YAML parser rejects this setup.
        passes, matches = False, []
        for rule in self.rules:
            if rule.cameras and event["camera_id"] not in rule.cameras:
                continue
            if rule.class_name != "*" and rule.class_name != event["class_name"]:
                continue
            if rule.zones and event["zone"] not in rule.zones:
                continue
            if rule.schedule and not rule.schedule.contains(_now_hhmm()):
                continue
            if (
                rule.min_confidence is not None
                and event["confidence"] < rule.min_confidence
            ):
                continue
            passes = True
            if not rule.notify:
                continue
            key = rule.name, event["camera_id"], event["zone"]
            now = time.monotonic()
            if (
                rule.cooldown_seconds > 0
                and now - self.last_fired.get(key, 0.0) < rule.cooldown_seconds
            ):
                continue
            self.last_fired[key] = now
            matches.append(
                {
                    "rule_name": rule.name,
                    "severity": rule.severity,
                    "message": format_message(rule.message, event),
                    "notifications_table": rule.notifications_table,
                    "detection": event,
                }
            )
        return matches if passes else None

    def evaluate(self, batch, camera):
        started = time.perf_counter()
        if batch["camera_id"] != camera.id or batch["model_id"] != camera.model_id:
            raise ValueError("event batch does not match assigned camera/model")
        accepted, alerts = [], []
        for original in batch["events"]:
            event = {**original, "zone": assign_zone(original, camera.zones)}
            matches = self.filter_and_tag(event)
            if matches is not None:
                accepted.append(event)
                alerts.extend(matches)
        context = {
            key: value
            for key, value in batch.items()
            if key not in ("message_type", "events")
        }
        context["rules"] = {
            "evaluated_at_ns": time.time_ns(),
            "evaluation_ms": (time.perf_counter() - started) * 1000,
            "input_events": len(batch["events"]),
            "accepted_events": len(accepted),
            "alert_count": len(alerts),
        }
        return (
            {**context, "message_type": "event.v1", "events": accepted},
            {**context, "message_type": "alert.v1", "alerts": alerts},
        )
