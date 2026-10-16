"""Bounded exact camera/stream/sequence joins; unmatched arrivals cannot block ready pairs."""

from collections import OrderedDict, deque
import threading
import time


def identity(header):
    return header["camera_id"], header["stream_id"], header["sequence"]


class Matcher:
    def __init__(
        self,
        camera_id,
        model_id,
        *,
        frame_max_items=64,
        frame_max_bytes=67108864,
        frame_max_age_ms=5000,
        max_items=32,
        max_bytes=4194304,
        max_age_ms=5000,
    ):
        self.camera_id, self.model_id = camera_id, model_id
        self.limits = {
            "frames": (frame_max_items, frame_max_bytes, frame_max_age_ms / 1000),
            "results": (max_items, max_bytes, max_age_ms / 1000),
        }
        self.caches = {name: OrderedDict() for name in self.limits}
        self.bytes = {name: 0 for name in self.limits}
        self.received = {name: 0 for name in self.limits}
        self.dropped = {name: 0 for name in self.limits}
        self.duplicates = self.rejected = self.matched = self.stale = 0
        self.stream = None
        self.sequence = self.capture_ns = 0
        self.retired = deque(maxlen=64)
        self.done = OrderedDict()
        self.lock = threading.Lock()

    def remove(self, name, key, drop=False):
        value = self.caches[name].pop(key)
        self.bytes[name] -= value[3]
        self.dropped[name] += int(drop)
        return value

    def expire(self):
        now = time.monotonic()
        for name, cache in self.caches.items():
            age = self.limits[name][2]
            while cache and age and now - next(iter(cache.values()))[2] > age:
                self.remove(name, next(iter(cache)), True)

    def put(self, name, header, image, size):
        with self.lock:
            self.received[name] += 1
            self.expire()
            if header["camera_id"] != self.camera_id or (
                name == "results" and header["model_id"] != self.model_id
            ):
                self.rejected += 1
                return
            key = identity(header)
            if key in self.done or key in self.caches[name]:
                self.duplicates += 1
                return
            if header["stream_id"] in self.retired or (
                header["stream_id"] == self.stream
                and header["sequence"] <= self.sequence
            ):
                self.stale += 1
                self.dropped[name] += 1
                return
            items, maximum, _ = self.limits[name]
            if size > maximum:
                self.dropped[name] += 1
                return
            while self.caches[name] and (
                len(self.caches[name]) >= items or self.bytes[name] + size > maximum
            ):
                self.remove(name, next(iter(self.caches[name])), True)
            self.caches[name][key] = header, image, time.monotonic(), size
            self.bytes[name] += size

    def put_frame(self, header, image):
        self.put("frames", header, image, image.nbytes)

    def put_result(self, header, size):
        self.put("results", header, None, size)

    def reject(self):
        with self.lock:
            self.rejected += 1

    def pop(self):
        with self.lock:
            self.expire()
            for key in list(self.caches["results"]):
                candidate = self.caches["results"][key][0]
                stale = candidate["stream_id"] in self.retired or (
                    candidate["stream_id"] == self.stream
                    and candidate["sequence"] <= self.sequence
                )
                if stale or (
                    self.stream is not None
                    and candidate["stream_id"] != self.stream
                    and candidate["received_at_ns"] < self.capture_ns
                ):
                    self.remove("results", key, True)
                    if key in self.caches["frames"]:
                        self.remove("frames", key, True)
                    self.stale += 1
                    continue
                if key not in self.caches["frames"]:
                    continue
                result = self.remove("results", key)[0]
                header, image, _, _ = self.remove("frames", key)
                self.done[key] = True
                if len(self.done) > 4096:
                    self.done.popitem(last=False)
                if any(
                    header[k] != result[k]
                    for k in ("received_at_ns", "width", "height")
                ):
                    self.rejected += 1
                    continue
                if self.stream is not None and self.stream != result["stream_id"]:
                    self.retired.append(self.stream)
                self.stream, self.sequence, self.capture_ns = (
                    result["stream_id"],
                    result["sequence"],
                    result["received_at_ns"],
                )
                self.matched += 1
                return header, image, result
        return None

    def clear(self):
        with self.lock:
            for name in self.caches:
                while self.caches[name]:
                    self.remove(name, next(iter(self.caches[name])), True)

    def snapshot(self):
        with self.lock:
            self.expire()
            return {
                "frames_received_total": self.received["frames"],
                "received_total": self.received["results"],
                "cached_frames": len(self.caches["frames"]),
                "cached_frame_bytes": self.bytes["frames"],
                "queued_batches": len(self.caches["results"]),
                "queued_bytes": self.bytes["results"],
                "frames_dropped_total": self.dropped["frames"],
                "dropped_total": self.dropped["results"],
                "duplicates_total": self.duplicates,
                "rejected_total": self.rejected,
                "matched_total": self.matched,
                "stale_total": self.stale,
            }
