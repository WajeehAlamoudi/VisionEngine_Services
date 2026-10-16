"""Bounded input correlation and serial, per-camera tracking."""

from collections import OrderedDict, deque
import logging
import threading
import time

from ..messaging.publisher import Publisher

log = logging.getLogger(__name__)


def key(header):
    return header["stream_id"], header["sequence"]


class Inbox:
    def __init__(
        self,
        requires_frame,
        max_items=32,
        max_bytes=4_194_304,
        max_age_ms=5000,
        frame_max_items=64,
        frame_max_bytes=67_108_864,
        frame_max_age_ms=5000,
    ):
        self.requires_frame = requires_frame
        self.max_items, self.max_bytes, self.max_age = (
            max_items,
            max_bytes,
            max_age_ms / 1000,
        )
        self.frame_max_items, self.frame_max_bytes = frame_max_items, frame_max_bytes
        self.frame_max_age = frame_max_age_ms / 1000
        self.batches, self.frames = OrderedDict(), OrderedDict()
        self.lock = threading.Lock()
        self.received = self.frames_received = self.dropped = self.frames_dropped = (
            self.rejected
        ) = 0
        self.batch_bytes = self.frame_bytes = 0

    def reject(self):
        with self.lock:
            self.rejected += 1

    def _remove(self, collection, name, *, dropped=True):
        _key, (_header, _image, _created, size) = collection.popitem(last=False)
        if name == "frames":
            self.frame_bytes -= size
            self.frames_dropped += int(dropped)
        else:
            self.batch_bytes -= size
            self.dropped += int(dropped)

    def _expire(self):
        now = time.monotonic()
        for collection, name, age in (
            (self.batches, "batches", self.max_age),
            (self.frames, "frames", self.frame_max_age),
        ):
            while collection and age and now - next(iter(collection.values()))[2] > age:
                self._remove(collection, name)

    def put_batch(self, batch, size):
        with self.lock:
            self.received += 1
            self._expire()
            if size > self.max_bytes or key(batch) in self.batches:
                self.dropped += 1
                return
            while self.batches and (
                len(self.batches) >= self.max_items
                or self.batch_bytes + size > self.max_bytes
            ):
                self._remove(self.batches, "batches")
            self.batches[key(batch)] = batch, None, time.monotonic(), size
            self.batch_bytes += size

    def put_frame(self, header, image):
        with self.lock:
            self.frames_received += 1
            self._expire()
            if image.nbytes > self.frame_max_bytes or key(header) in self.frames:
                self.frames_dropped += 1
                return
            while self.frames and (
                len(self.frames) >= self.frame_max_items
                or self.frame_bytes + image.nbytes > self.frame_max_bytes
            ):
                self._remove(self.frames, "frames")
            self.frames[key(header)] = header, image, time.monotonic(), image.nbytes
            self.frame_bytes += image.nbytes

    def pop(self):
        with self.lock:
            self._expire()
            if not self.batches:
                return None
            identity = next(iter(self.batches))
            batch = self.batches[identity][0]
            image = None
            if self.requires_frame:
                frame = self.frames.get(identity)
                if frame is None:
                    return None  # Preserve detection order until matched or expired.
                header, image, _created, size = self.frames.pop(identity)
                self.frame_bytes -= size
                if any(
                    header[field] != batch[field]
                    for field in ("received_at_ns", "width", "height")
                ):
                    self._remove(self.batches, "batches")
                    self.rejected += 1
                    return None
            self._remove(self.batches, "batches", dropped=False)
            return batch, image

    def snapshot(self):
        with self.lock:
            self._expire()
            return {
                "received_total": self.received,
                "frames_received_total": self.frames_received,
                "queued_batches": len(self.batches),
                "queued_bytes": self.batch_bytes,
                "cached_frames": len(self.frames),
                "cached_frame_bytes": self.frame_bytes,
                "dropped_total": self.dropped,
                "frames_dropped_total": self.frames_dropped,
                "rejected_total": self.rejected,
            }


class TrackingWorker(threading.Thread):
    def __init__(self, tracker, inbox, stop, bind, *, max_frames=None, idle_reset=30):
        super().__init__(name="tracking-worker", daemon=True)
        self.tracker, self.inbox, self.stop, self.bind = tracker, inbox, stop, bind
        self.max_frames, self.idle_reset = max_frames, idle_reset
        self.error = None
        self.lock = threading.Lock()
        self.status = {
            "state": "loading",
            "since": time.monotonic(),
            "processed": 0,
            "processed_at_ns": 0,
            "resets": 0,
            "sequence_gaps": 0,
            "stale_total": 0,
            "sent_total": 0,
        }

    def state(self, value, **fields):
        with self.lock:
            self.status.update(state=value, since=time.monotonic(), **fields)

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def run(self):
        publisher = None
        stream = version = shape = None
        sequence = timestamp = 0
        retired_streams = deque(maxlen=64)
        last_update = time.monotonic()
        try:
            self.tracker.load()
            publisher = Publisher(self.bind)
            log.info(
                "tracker ready",
                extra={
                    "event": "tracker_ready",
                    "fields": {
                        "algorithm": self.tracker.config.algorithm,
                        "requires_frame": self.inbox.requires_frame,
                        "bind": self.bind,
                    },
                },
            )
            self.state("waiting")
            while not self.stop.is_set():
                item = self.inbox.pop()
                if item is None:
                    self.stop.wait(0.01)
                    continue
                batch, image = item
                new_stream = batch["stream_id"] != stream
                # Capture timestamps fence delayed messages from an older session.
                if (
                    new_stream
                    and (
                        batch["received_at_ns"] < timestamp
                        or batch["stream_id"] in retired_streams
                    )
                ) or (not new_stream and batch["sequence"] <= sequence):
                    with self.lock:
                        self.status["stale_total"] += 1
                    continue
                dimensions = batch["width"], batch["height"]
                reset = stream is not None and (
                    new_stream
                    or version != batch["model_version"]
                    or shape != dimensions
                    or time.monotonic() - last_update > self.idle_reset
                )
                self.state("loading" if reset else "tracking")
                if reset:
                    if new_stream:
                        retired_streams.append(stream)
                    self.tracker.reset()
                    self.tracker.load()
                    with self.lock:
                        self.status["resets"] += 1
                    log.info(
                        "tracking state reset",
                        extra={
                            "event": "tracker_reset",
                            "fields": {"stream_id": batch["stream_id"]},
                        },
                    )
                if stream == batch["stream_id"]:
                    with self.lock:
                        self.status["sequence_gaps"] += max(
                            0, batch["sequence"] - sequence - 1
                        )
                stream, version, shape = (
                    batch["stream_id"],
                    batch["model_version"],
                    dimensions,
                )
                sequence, timestamp = batch["sequence"], batch["received_at_ns"]
                self.state("tracking")
                output = self.tracker.update(batch, image)
                publisher.publish(output)
                last_update = time.monotonic()
                with self.lock:
                    self.status["processed"] += 1
                    self.status["sent_total"] += 1
                    self.status["processed_at_ns"] = time.time_ns()
                    self.status["last_tracking_ms"] = output["tracking"]["tracking_ms"]
                log.debug(
                    "tracks sent",
                    extra={
                        "stage": "output",
                        "event": "tracks_sent",
                        "fields": {
                            "stream_id": stream,
                            "sequence": sequence,
                            "tracks": len(output["detections"]),
                        },
                    },
                )
                self.state("waiting")
                if self.max_frames and self.snapshot()["processed"] >= self.max_frames:
                    break
        except Exception as exc:
            self.error = exc
            log.exception("tracking worker failed")
        finally:
            self.state("closing")
            if publisher:
                publisher.close()
            self.state("failed" if self.error else "stopped")
            self.stop.set()
