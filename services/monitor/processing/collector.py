"""Collect bounded health snapshots; preserve unknown measurements explicitly."""

from datetime import datetime, timezone
import json
import math
import time

MAX_SNAPSHOT_BYTES = 1024 * 1024


def read_snapshot(path, *, now, max_age):
    try:
        if not path.is_file():
            raise ValueError("snapshot must be a regular file")
        with path.open("rb") as stream:
            payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds size limit")
        snapshot = json.loads(payload)
        # A writer may update its snapshot while this collection pass is reading.
        now = max(now, time.time())
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a mapping")
        updated = snapshot.get("updated_at")
        if type(updated) not in (int, float) or not math.isfinite(updated):
            raise ValueError("missing snapshot timestamp")
        if not 0 <= now - updated <= max_age:
            raise ValueError("snapshot timestamp is stale or in the future")
        state = snapshot.get("state")
        if not isinstance(state, str) or state in ("failed", "stopped", "closing"):
            raise ValueError("service is not running")
        for key in (
            "per_camera",
            "per_camera_accepted",
            "per_camera_processed_at_ns",
            "inputs",
        ):
            if key in snapshot and not isinstance(snapshot[key], dict):
                raise ValueError("invalid service counters")
        progress = snapshot.get("processed_at_ns", snapshot.get("received_at_ns", 0))
        if type(progress) is not int or not 0 <= now - progress / 1e9 <= max_age:
            raise ValueError("service has no recent completed work")
        # NaN/Infinity must never enter health wire metadata.
        json.dumps(snapshot, allow_nan=False)
        return snapshot, "service dependency failure" if snapshot.get(
            "dependency_error"
        ) else None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        # Paths and parser messages may contain credentials; report a fixed reason.
        return None, "missing, invalid, stale or non-progressing health snapshot"


class Collector:
    def __init__(
        self,
        device,
        cameras,
        sources,
        stages,
        counters,
        *,
        max_age=30,
        buffer_source="buffer",
    ):
        self.buffer_source = buffer_source
        self.device, self.cameras = device, cameras
        self.sources, self.stages, self.counters = sources, stages, counters
        self.max_age = max_age
        self.started = time.monotonic()
        self.totals = {key: 0 for key in cameras}
        self.previous = {}
        self.rates = {}

    def collect(self):
        now = time.time()
        services, raw = {}, {}
        for name, path in self.sources.items():
            snapshot, error = read_snapshot(path, now=now, max_age=self.max_age)
            raw[name] = snapshot
            services[name] = {
                "status": "error" if error else "ok",
                "last_error": error,
                "metrics": snapshot if snapshot is not None else {},
            }
            if snapshot is not None:
                previous = self.rates.get(name)
                rates = {}
                for key in (
                    "processed",
                    "captured",
                    "published",
                    "sent_total",
                    "rows_total",
                    "accepted_total",
                    "events_total",
                ):
                    value = snapshot.get(key)
                    if (
                        type(value) is int
                        and previous
                        and type(previous[1].get(key)) is int
                        and now > previous[0]
                    ):
                        rates[key + "_per_second"] = round(
                            max(0, value - previous[1][key]) / (now - previous[0]), 2
                        )
                services[name]["rates"] = rates
                self.rates[name] = now, snapshot
        now = max(now, time.time())
        cameras = []
        for key, camera in self.cameras.items():
            required = self.stages.get(key, [])
            errors = [name for name in required if services[name]["status"] != "ok"]
            if not required:
                errors.append("no monitored stages assigned")
            source = self.counters.get(key)
            snapshot = raw.get(source)
            value = (
                snapshot.get("per_camera_accepted", {}).get(key) if snapshot else None
            )
            available = type(value) is int and value >= 0
            progress = (
                snapshot.get("per_camera_processed_at_ns", {}).get(key, 0)
                if snapshot
                else 0
            )
            if available and (
                type(progress) is not int
                or not 0 <= now - progress / 1e9 <= self.max_age
            ):
                errors.append("camera has no recent rule evaluation")
            if not available:
                errors.append("accepted-detection counter unavailable")
            if available:
                epoch = snapshot.get("session_id")
                previous = self.previous.get(key)
                delta = (
                    value
                    if previous is None or epoch != previous[0] or value < previous[1]
                    else value - previous[1]
                )
                self.totals[key] += delta
                self.previous[key] = epoch, value
            cameras.append(
                {
                    "id": key,
                    "name": camera.name,
                    "status": "error" if errors else "ok",
                    "last_error": ", ".join(errors) if errors else None,
                    "detections_total": self.totals[key],
                    "counter_available": available,
                    "counter_source": source,
                    "stages": required,
                    "frames_processed": snapshot.get("per_camera", {}).get(key, 0)
                    if snapshot
                    else 0,
                }
            )
        active = sum(camera["status"] == "ok" for camera in cameras)
        errors = len(cameras) - active
        status = "ok" if not errors else "error" if not active else "degraded"
        if status == "ok" and any(s["status"] != "ok" for s in services.values()):
            status = "degraded"
        # A future buffer source must publish an actual pending count; none exists yet.
        buffer = {"available": False, "pending": None}
        if self.buffer_source in raw and raw[self.buffer_source] is not None:
            pending = raw[self.buffer_source].get("pending")
            if type(pending) is int and pending >= 0:
                buffer = {"available": True, "pending": pending}
        return {
            "device_id": self.device.id,
            "name": self.device.name,
            "location": self.device.location,
            "status": status,
            "uptime_seconds": int(time.monotonic() - self.started),
            "cameras_active": active,
            "cameras_error": errors,
            "cameras": cameras,
            "buffer": buffer,
            "services": services,
            "ts": datetime.fromtimestamp(now, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
