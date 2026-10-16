"""Fair per-camera queues feeding one serial YOLO model and its outputs."""

from collections import deque
import logging
import threading
import time

from services.inference.messaging.frames import Frame
from ..messaging.publisher import Publisher
from .annotator import annotate

log = logging.getLogger(__name__)


class FrameInbox:
    def __init__(self, inputs, max_items=2, max_bytes=16_777_216, max_age_ms=500):
        self.inputs, self.max_items, self.max_bytes = inputs, max_items, max_bytes
        self.max_age = max_age_ms / 1000
        self.queues = {key: deque() for key in inputs}
        self.bytes = dict.fromkeys(inputs, 0)
        self.order = deque(inputs)
        self.next_due = dict.fromkeys(inputs, 0.0)
        self.last_frame = {}
        self.dropped = dict.fromkeys(inputs, 0)
        self.received = dict.fromkeys(inputs, 0)
        self.condition = threading.Condition()

    def _remove(self, key):
        header, image, arrived = self.queues[key].popleft()
        self.bytes[key] -= image.nbytes
        return header, image, arrived

    def put(self, header, image, arrived):
        key = header["camera_id"]
        with self.condition:
            self.received[key] += 1
            identity = (header["stream_id"], header["sequence"])
            previous = self.last_frame.get(key)
            if previous and previous[0] == identity[0] and identity[1] <= previous[1]:
                self.dropped[key] += 1
                return
            if previous and previous[0] != identity[0]:
                self.dropped[key] += len(self.queues[key])
                self.queues[key].clear()
                self.bytes[key] = 0
                self.next_due[key] = 0
            self.last_frame[key] = identity
            if image.nbytes > self.max_bytes:
                self.dropped[key] += 1
                return
            while self.queues[key] and (
                len(self.queues[key]) >= self.max_items
                or self.bytes[key] + image.nbytes > self.max_bytes
            ):
                self._remove(key)
                self.dropped[key] += 1
            self.queues[key].append((header, image, arrived))
            self.bytes[key] += image.nbytes
            self.condition.notify_all()

    def get(self, stop):
        with self.condition:
            while not stop.is_set():
                now = time.monotonic()
                wait = 0.02
                for _ in range(len(self.order)):
                    key = self.order[0]
                    self.order.rotate(-1)
                    while (
                        self.queues[key]
                        and self.max_age
                        and now - self.queues[key][0][2] > self.max_age
                    ):
                        self._remove(key)
                        self.dropped[key] += 1
                    if self.queues[key] and now >= self.next_due[key]:
                        interval = 1 / self.inputs[key].camera.fps_target
                        # Preserve the scheduling phase across small wake-up delays.
                        # After a full missed interval, resume without catch-up bursts.
                        following = self.next_due[key] + interval
                        self.next_due[key] = (
                            following
                            if self.next_due[key] and following > now
                            else now + interval
                        )
                        return self._remove(key)
                    if self.queues[key]:
                        wait = min(wait, max(0, self.next_due[key] - now))
                self.condition.wait(wait)
        return None

    def snapshot(self):
        with self.condition:
            return {
                key: {
                    "queued": len(self.queues[key]),
                    "queued_bytes": self.bytes[key],
                    "dropped_local": self.dropped[key],
                    "received": self.received[key],
                }
                for key in self.inputs
            }


class InferenceWorker(threading.Thread):
    def __init__(
        self, model, inputs, inbox, stop, *, bind, annotated_bind=None, max_frames=None
    ):
        super().__init__(name="inference-model", daemon=True)
        self.model, self.inputs, self.inbox, self.stop = model, inputs, inbox, stop
        self.bind, self.annotated_bind, self.max_frames = (
            bind,
            annotated_bind,
            max_frames,
        )
        self.lock = threading.Lock()
        self.status = {
            "state": "created",
            "since": time.monotonic(),
            "processed": 0,
            "processed_at_ns": 0,
        }
        self.per_camera = dict.fromkeys(inputs, 0)
        self.error = None
        self.annotated_sent = dict.fromkeys(inputs, 0)
        self.last_inference_ms = dict.fromkeys(inputs, 0.0)

    def _state(self, state):
        with self.lock:
            self.status.update(state=state, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            return {
                **self.status,
                "per_camera": dict(self.per_camera),
                "model_id": self.model.config.id,
                "annotated_sent": dict(self.annotated_sent),
                "last_inference_ms": dict(self.last_inference_ms),
            }

    def run(self):
        publisher = None
        try:
            self._state("loading")
            self.model.load()
            publisher = Publisher(self.bind, self.annotated_bind)
            log.info(
                "model %s loaded once; serving %s cameras",
                self.model.config.id,
                len(self.inputs),
            )
            while not self.stop.is_set():
                self._state("waiting")
                item = self.inbox.get(self.stop)
                if item is None:
                    break
                header, image, _ = item
                key = header["camera_id"]
                self._state("inferring")
                detections, elapsed = self.model.infer(
                    image, self.inputs[key].camera.classes
                )
                log.debug(
                    "prediction complete",
                    extra={
                        "stage": "processing",
                        "event": "prediction_complete",
                        "fields": {
                            "camera_id": key,
                            "stream_id": header["stream_id"],
                            "sequence": header["sequence"],
                            "inference_ms": elapsed,
                            "detections": len(detections),
                        },
                    },
                )
                batch = {
                    field: header[field]
                    for field in (
                        "camera_id",
                        "stream_id",
                        "sequence",
                        "received_at_ns",
                        "width",
                        "height",
                    )
                }
                batch.update(
                    message_type="detections.v1",
                    model_id=self.model.config.id,
                    model_version=self.model.config.version,
                    processed_at_ns=time.time_ns(),
                    inference_ms=elapsed,
                    detections=detections,
                )
                annotated = None
                if self.annotated_bind:
                    annotated = Frame(
                        key,
                        header["stream_id"],
                        header["sequence"],
                        header["received_at_ns"],
                        annotate(image, detections),
                        time.monotonic(),
                        {
                            "stage": "inference",
                            "model_id": self.model.config.id,
                            "model_version": self.model.config.version,
                            "detection_count": len(detections),
                        },
                    )
                publisher.publish(batch, annotated)
                with self.lock:
                    self.status["processed"] += 1
                    self.status["processed_at_ns"] = batch["processed_at_ns"]
                    self.per_camera[key] += 1
                    self.last_inference_ms[key] = elapsed
                    if annotated is not None:
                        self.annotated_sent[key] += 1
                    count = self.status["processed"]
                if self.max_frames and count >= self.max_frames:
                    break
        except Exception as exc:
            self.error = type(exc).__name__
            log.error("inference failed (%s): %s", self.error, str(exc)[:300])
        finally:
            self._state("closing")
            try:
                if publisher:
                    publisher.close()
            except Exception as exc:
                self.error = type(exc).__name__
                log.error("publisher cleanup failed (%s)", self.error)
            finally:
                try:
                    self.model.close()
                except Exception as exc:
                    self.error = type(exc).__name__
                    log.error("model cleanup failed (%s)", self.error)
                finally:
                    self._state("failed" if self.error else "stopped")
                    self.stop.set()
