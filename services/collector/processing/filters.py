"""Capture-time schedules, original class/confidence filters and bounded sampling."""

from datetime import datetime, timezone, time as datetime_time, date
import random
import time


def in_schedule(schedule, capture_ns):
    now = datetime.fromtimestamp(capture_ns / 1e9, timezone.utc)
    day = now.date()
    clock = now.time().replace(tzinfo=None)
    return not (
        (schedule.start_date and day < date.fromisoformat(schedule.start_date))
        or (schedule.end_date and day > date.fromisoformat(schedule.end_date))
        or (schedule.after and clock < datetime_time.fromisoformat(schedule.after))
        or (schedule.before and clock > datetime_time.fromisoformat(schedule.before))
    )


def apply_filters(detections, filters):
    return [
        d
        for d in detections
        if (not filters.classes or d["class_name"] in filters.classes)
        and d["confidence"] >= filters.min_confidence
    ]


class Sampler:
    def __init__(self, config):
        self.config = config
        self.next_due = None

    def accepts(self, detections, now=None):
        if self.config.mode == "on_detection":
            return bool(detections)
        now = time.monotonic() if now is None else now
        return self.next_due is None or now >= self.next_due

    def saved(self, now=None):
        now = time.monotonic() if now is None else now
        if self.config.mode == "interval":
            self.next_due = now + self.config.interval_seconds
        elif self.config.mode == "random":
            self.next_due = now + 60 / self.config.frames_per_minute * random.uniform(
                0.5, 1.5
            )
