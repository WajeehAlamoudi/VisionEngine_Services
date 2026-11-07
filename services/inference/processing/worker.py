"""Fair per-camera queues feeding one serial YOLO model and its outputs."""

from collections import deque
import logging
import threading
import time

from services.inference.messaging.frames import Frame
from ..messaging.publisher import AnnotatedPublisher, Publisher
from .annotator import annotate

log = logging.getLogger(__name__)


class FrameInbox:
    def __init__(self, inputs, max_items=2, max_bytes=16_777_216, max_age_ms=500):
        self.inputs, self.max_items, self.max_bytes = inputs, max_items, max_bytes
        self.max_age = max_age_ms / 1000
        self.queues = {key: deque() for key in inputs}
        self.bytes = dict.fromkeys(inputs, 0)
        self.order = deque(inputs)
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
                    if self.queues[key]:
                        return self._remove(key)
                self.condition.wait(0.02)
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


class AnnotationWorker(threading.Thread):
    """Best-effort previews: one latest pending job per camera, plus one in flight."""

    def __init__(self, inputs, endpoint):
        super().__init__(name="inference-annotation", daemon=True)
        self.endpoint = endpoint
        self.condition = threading.Condition()
        self.order = deque(inputs)
        self.pending = {}
        self.stopping = False
        self.error = None
        self.state, self.since = "created", time.monotonic()
        self.inflight = None
        self.counters = {
            key: dict(sent_total=0, skipped_total=0, failed_total=0, cancelled_total=0)
            for key in inputs
        }
        self.metrics = {key: self._empty_metrics() for key in inputs}
        self.metrics_since = time.monotonic()

    @staticmethod
    def _empty_metrics():
        return dict.fromkeys(
            (
                "annotation_frames",
                "annotation_queue_wait_ms_max",
                "annotation_duration_ms_max",
                "annotated_publish_duration_ms_max",
            ),
            0,
        )

    def submit(self, batch, image):
        key = batch["camera_id"]
        with self.condition:
            if self.stopping or self.error:
                self.counters[key]["skipped_total"] += 1
                return
            if key in self.pending:
                self.counters[key]["skipped_total"] += 1
            self.pending[key] = batch, image, time.monotonic()
            self.condition.notify()

    def request_stop(self):
        with self.condition:
            self.stopping = True
            for key in self.pending:
                self.counters[key]["cancelled_total"] += 1
            self.pending.clear()
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {
                "enabled": True,
                "state": self.state,
                "since": self.since,
                "error": self.error,
                "alive": self.is_alive(),
                "inflight_camera": self.inflight,
                "per_camera": {
                    key: {**counts, "queued": int(key in self.pending)}
                    for key, counts in self.counters.items()
                },
            }

    def take_metrics(self):
        with self.condition:
            now = time.monotonic()
            result = {
                key: {
                    **values,
                    "annotation_enabled": True,
                    "annotation_sample_elapsed_ms": (now - self.metrics_since) * 1000,
                    "annotation_queued_frames": int(key in self.pending),
                    **{
                        "annotation_" + name: count
                        for name, count in self.counters[key].items()
                    },
                }
                for key, values in self.metrics.items()
            }
            self.metrics = {key: self._empty_metrics() for key in self.counters}
            self.metrics_since = now
            return result

    def run(self):
        publisher = None
        try:
            # The preview thread owns its socket/context from creation to close.
            publisher = AnnotatedPublisher(self.endpoint)
            while True:
                with self.condition:
                    self.state, self.since = "waiting", time.monotonic()
                    while not self.pending and not self.stopping:
                        self.condition.wait()
                    if self.stopping:
                        break
                    for _ in range(len(self.order)):
                        key = self.order[0]
                        self.order.rotate(-1)
                        if key in self.pending:
                            batch, image, arrived = self.pending.pop(key)
                            break
                    self.inflight = key
                    self.state, self.since = "annotating", time.monotonic()
                queue_wait_ms = (time.monotonic() - arrived) * 1000
                started = time.perf_counter()
                frame = Frame(
                    key,
                    batch["stream_id"],
                    batch["sequence"],
                    batch["received_at_ns"],
                    annotate(image, batch["detections"]),
                    time.monotonic(),
                    {
                        "stage": "inference",
                        "model_id": batch["model_id"],
                        "model_version": batch["model_version"],
                        "detection_count": len(batch["detections"]),
                    },
                )
                annotated = time.perf_counter()
                publisher.publish(frame)
                finished = time.perf_counter()
                with self.condition:
                    self.counters[key]["sent_total"] += 1
                    metrics = self.metrics[key]
                    metrics["annotation_frames"] += 1
                    for field, value in (
                        ("annotation_queue_wait_ms_max", queue_wait_ms),
                        ("annotation_duration_ms_max", (annotated - started) * 1000),
                        (
                            "annotated_publish_duration_ms_max",
                            (finished - annotated) * 1000,
                        ),
                    ):
                        metrics[field] = max(metrics[field], value)
                    self.inflight = None
        except Exception as exc:
            with self.condition:
                self.error = type(exc).__name__
                if self.inflight is not None:
                    self.counters[self.inflight]["failed_total"] += 1
            log.error(
                "annotated output failed (%s); detection processing continues",
                self.error,
            )
        finally:
            self.request_stop()
            try:
                if publisher is not None:
                    publisher.close()
            except Exception as exc:
                self.error = type(exc).__name__
                log.error("annotated output cleanup failed (%s)", self.error)
            with self.condition:
                self.inflight = None
                self.state = "failed" if self.error else "stopped"
                self.since = time.monotonic()


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
        self.annotation_worker = (
            AnnotationWorker(inputs, annotated_bind) if annotated_bind else None
        )
        self.last_inference_ms = dict.fromkeys(inputs, 0.0)
        self.metrics = {key: self._empty_metrics() for key in inputs}
        self.metrics_since = time.monotonic()

    @staticmethod
    def _empty_metrics():
        return dict.fromkeys(
            (
                "frames",
                "detections",
                "detections_per_frame_max",
                "queue_wait_ms_max",
                "prediction_duration_ms_max",
                "publish_duration_ms_max",
                "worker_duration_ms_max",
            ),
            0,
        )

    def take_metrics(self):
        with self.lock:
            now = time.monotonic()
            result = {
                key: {
                    **values,
                    "sample_elapsed_ms": (now - self.metrics_since) * 1000,
                    "detections_per_frame_mean": values["detections"] / values["frames"]
                    if values["frames"]
                    else 0,
                }
                for key, values in self.metrics.items()
            }
            self.metrics = {key: self._empty_metrics() for key in self.inputs}
            self.metrics_since = now
        if self.annotation_worker is not None:
            for key, values in self.annotation_worker.take_metrics().items():
                result[key].update(values)
        return result

    def _state(self, state):
        with self.lock:
            self.status.update(state=state, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            result = {
                **self.status,
                "per_camera": dict(self.per_camera),
                "model_id": self.model.config.id,
                "last_inference_ms": dict(self.last_inference_ms),
            }
        annotation = (
            self.annotation_worker.snapshot()
            if self.annotation_worker
            else {"enabled": False, "state": "disabled", "per_camera": {}}
        )
        result["annotation"] = annotation
        result["annotated_sent"] = {
            key: annotation["per_camera"].get(key, {}).get("sent_total", 0)
            for key in self.inputs
        }
        return result

    def join_annotation(self, timeout):
        if (
            self.annotation_worker is not None
            and self.annotation_worker.ident is not None
        ):
            self.annotation_worker.join(timeout)

    def annotation_alive(self):
        return self.annotation_worker is not None and self.annotation_worker.is_alive()

    def run(self):
        publisher = None
        try:
            self._state("loading")
            self.model.load()
            publisher = Publisher(self.bind)
            if self.annotation_worker is not None:
                self.annotation_worker.start()
            log.info(
                "model %s loaded; serving %s cameras",
                self.model.config.id,
                len(self.inputs),
                extra={
                    "stage": "processing",
                    "event": "model_loaded",
                },
            )
            while not self.stop.is_set():
                self._state("waiting")
                item = self.inbox.get(self.stop)
                if item is None:
                    break
                header, image, arrived = item
                queue_wait_ms = (time.monotonic() - arrived) * 1000
                started = time.perf_counter()
                key = header["camera_id"]
                self._state("inferring")
                detections, elapsed = self.model.infer(
                    image, self.inputs[key].camera.classes
                )
                predicted = time.perf_counter()
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
                publish_started = time.perf_counter()
                publisher.publish(batch)
                published = time.perf_counter()
                if self.annotation_worker is not None:
                    self.annotation_worker.submit(batch, image)
                finished = time.perf_counter()
                with self.lock:
                    metrics = self.metrics[key]
                    metrics["frames"] += 1
                    metrics["detections"] += len(detections)
                    metrics["detections_per_frame_max"] = max(
                        metrics["detections_per_frame_max"], len(detections)
                    )
                    for field, value in (
                        ("queue_wait_ms_max", queue_wait_ms),
                        ("prediction_duration_ms_max", (predicted - started) * 1000),
                        (
                            "publish_duration_ms_max",
                            (published - publish_started) * 1000,
                        ),
                        ("worker_duration_ms_max", (finished - started) * 1000),
                    ):
                        metrics[field] = max(metrics[field], value)
                    self.status["processed"] += 1
                    self.status["processed_at_ns"] = batch["processed_at_ns"]
                    self.per_camera[key] += 1
                    self.last_inference_ms[key] = elapsed
                    count = self.status["processed"]
                if self.max_frames and count >= self.max_frames:
                    break
        except Exception as exc:
            self.error = type(exc).__name__
            log.error("inference failed (%s): %s", self.error, str(exc)[:300])
        finally:
            self._state("closing")
            if self.annotation_worker is not None:
                self.annotation_worker.request_stop()
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
