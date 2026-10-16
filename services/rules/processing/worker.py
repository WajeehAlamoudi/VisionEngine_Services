"""Fair per-camera input queues and one serial rules engine."""

from collections import deque
import logging
import threading
import time
import uuid

from ..messaging.publisher import Publisher

log = logging.getLogger(__name__)


class CameraQueue:
    def __init__(self, max_items=32, max_bytes=4_194_304, max_age_ms=5000):
        self.max_items, self.max_bytes, self.max_age = (
            max_items,
            max_bytes,
            max_age_ms / 1000,
        )
        self.items = deque()
        self.lock = threading.Lock()
        self.bytes = self.received = self.dropped = self.rejected = 0

    def _drop(self):
        self.bytes -= self.items.popleft()[2]
        self.dropped += 1

    def _expire(self):
        while (
            self.items
            and self.max_age
            and time.monotonic() - self.items[0][1] > self.max_age
        ):
            self._drop()

    def put(self, batch, size):
        with self.lock:
            self.received += 1
            self._expire()
            if size > self.max_bytes:
                self.dropped += 1
                return
            while self.items and (
                len(self.items) >= self.max_items or self.bytes + size > self.max_bytes
            ):
                self._drop()
            self.items.append((batch, time.monotonic(), size))
            self.bytes += size

    def pop(self):
        with self.lock:
            self._expire()
            if not self.items:
                return None
            batch, _created, size = self.items.popleft()
            self.bytes -= size
            return batch

    def reject(self):
        with self.lock:
            self.rejected += 1

    def snapshot(self):
        with self.lock:
            self._expire()
            return {
                "received_total": self.received,
                "queued_batches": len(self.items),
                "queued_bytes": self.bytes,
                "dropped_total": self.dropped,
                "rejected_total": self.rejected,
            }


class Inbox:
    def __init__(self, inputs, max_items=32, max_bytes=4_194_304, max_age_ms=5000):
        self.max_items = max_items
        self.queues = {
            key: CameraQueue(max_items, max_bytes, max_age_ms) for key in inputs
        }
        self.order = deque(inputs)

    def put(self, batch, size):
        self.queues[batch["camera_id"]].put(batch, size)

    def reject(self, camera_id):
        self.queues[camera_id].reject()

    def pop(self):
        for _ in range(len(self.order)):
            key = self.order[0]
            self.order.rotate(-1)
            batch = self.queues[key].pop()
            if batch is not None:
                return batch
        return None

    def snapshot(self):
        return {key: queue.snapshot() for key, queue in self.queues.items()}


class RulesWorker(threading.Thread):
    def __init__(
        self, engine, inputs, inbox, stop, bind, alerts_bind, *, max_frames=None
    ):
        super().__init__(name="rules-worker", daemon=True)
        self.engine, self.inputs, self.inbox, self.stop = engine, inputs, inbox, stop
        self.bind, self.alerts_bind, self.max_frames = bind, alerts_bind, max_frames
        self.error = None
        self.lock = threading.Lock()
        self.status = {
            "session_id": uuid.uuid4().hex,
            "state": "loading",
            "since": time.monotonic(),
            "processed": 0,
            "processed_at_ns": 0,
            "accepted_total": 0,
            "alerts_total": 0,
            "rejected_events_total": 0,
            "sent_total": 0,
            "stale_total": 0,
            "last_evaluation_ms": 0,
        }
        self.per_camera = dict.fromkeys(inputs, 0)
        self.per_camera_accepted = dict.fromkeys(inputs, 0)
        self.per_camera_processed_at_ns = dict.fromkeys(inputs, 0)

    def state(self, value):
        with self.lock:
            self.status.update(state=value, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            return {
                **self.status,
                "per_camera": dict(self.per_camera),
                "per_camera_accepted": dict(self.per_camera_accepted),
                "per_camera_processed_at_ns": dict(self.per_camera_processed_at_ns),
            }

    def run(self):
        publisher = None
        sessions = {}
        retired = {key: deque(maxlen=64) for key in self.inputs}
        try:
            publisher = Publisher(self.bind, self.alerts_bind)
            log.info(
                "rules ready",
                extra={
                    "event": "service_ready",
                    "fields": {
                        "accepted_bind": self.bind,
                        "alerts_bind": self.alerts_bind,
                    },
                },
            )
            self.state("waiting")
            while not self.stop.is_set():
                batch = self.inbox.pop()
                if batch is None:
                    self.stop.wait(0.01)
                    continue
                key = batch["camera_id"]
                previous = sessions.get(key)
                new_stream = previous is None or batch["stream_id"] != previous[0]
                if previous and (
                    (
                        new_stream
                        and (
                            batch["stream_id"] in retired[key]
                            or batch["received_at_ns"] < previous[2]
                        )
                    )
                    or (not new_stream and batch["sequence"] <= previous[1])
                ):
                    with self.lock:
                        self.status["stale_total"] += 1
                    continue
                if previous and new_stream:
                    retired[key].append(previous[0])
                sessions[key] = (
                    batch["stream_id"],
                    batch["sequence"],
                    batch["received_at_ns"],
                )
                self.state("evaluating")
                accepted, alerts = self.engine.evaluate(batch, self.inputs[key].camera)
                publisher.publish(accepted, alerts)
                with self.lock:
                    self.status["processed"] += 1
                    self.status["sent_total"] += 1
                    self.status["accepted_total"] += len(accepted["events"])
                    self.status["alerts_total"] += len(alerts["alerts"])
                    self.status["rejected_events_total"] += len(batch["events"]) - len(
                        accepted["events"]
                    )
                    self.status["last_evaluation_ms"] = accepted["rules"][
                        "evaluation_ms"
                    ]
                    self.status["processed_at_ns"] = time.time_ns()
                    self.per_camera[key] += 1
                    self.per_camera_accepted[key] += len(accepted["events"])
                    self.per_camera_processed_at_ns[key] = self.status[
                        "processed_at_ns"
                    ]
                log.debug(
                    "rule outputs sent",
                    extra={
                        "stage": "output",
                        "event": "rule_outputs_sent",
                        "fields": {
                            "camera_id": key,
                            "stream_id": batch["stream_id"],
                            "sequence": batch["sequence"],
                            "accepted": len(accepted["events"]),
                            "alerts": len(alerts["alerts"]),
                        },
                    },
                )
                self.state("waiting")
                if self.max_frames and self.snapshot()["processed"] >= self.max_frames:
                    break
        except Exception as exc:
            self.error = exc
            log.exception("rules worker failed")
        finally:
            self.state("closing")
            if publisher:
                publisher.close()
            self.state("failed" if self.error else "stopped")
            self.stop.set()
