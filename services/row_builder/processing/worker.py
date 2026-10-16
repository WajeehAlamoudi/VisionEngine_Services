"""Fair per-camera input queues and one serial row builder."""

from collections import deque
import logging
import threading
import time

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

    def put(self, key, batch, size):
        self.queues[key].put(batch, size)

    def reject(self, camera_id):
        self.queues[camera_id].reject()

    def pop(self):
        for _ in range(len(self.order)):
            key = self.order[0]
            self.order.rotate(-1)
            batch = self.queues[key].pop()
            if batch is not None:
                return key, batch
        return None

    def snapshot(self):
        return {key: queue.snapshot() for key, queue in self.queues.items()}


class RowWorker(threading.Thread):
    def __init__(
        self, builder, inputs, inbox, stop, bind, *, max_frames=None, journal=None
    ):
        super().__init__(name="row-builder", daemon=True)
        self.builder, self.inputs, self.inbox, self.stop = builder, inputs, inbox, stop
        self.bind, self.max_frames = bind, max_frames
        self.error = None
        self.journal = journal
        self.lock = threading.Lock()
        self.status = {
            "state": "loading",
            "since": time.monotonic(),
            "processed": 0,
            "processed_at_ns": 0,
            "sent_total": 0,
            "rows_total": 0,
            "detection_rows_total": 0,
            "notification_rows_total": 0,
            "node_rows_total": 0,
            "skipped_disabled_total": 0,
            "skipped_no_table_total": 0,
            "stale_total": 0,
            "last_build_ms": 0,
        }

    def state(self, value):
        with self.lock:
            self.status.update(state=value, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def run(self):
        publisher = None
        sessions, retired = {}, {}
        try:
            publisher = Publisher(self.bind)
            log.info(
                "row publisher ready",
                extra={
                    "event": "service_ready",
                    "fields": {"bind": self.bind},
                },
            )
            self.state("waiting")
            while not self.stop.is_set():
                incoming = self.inbox.pop()
                if incoming is None:
                    self.stop.wait(0.01)
                    continue
                source, batch = incoming
                # Events and alerts share sequence IDs but are independent inputs.
                identity = batch.get("camera_id", batch.get("device_id"))
                key = source, identity
                previous = sessions.get(key)
                old_streams = retired.setdefault(key, deque(maxlen=64))
                new_stream = previous is None or batch["stream_id"] != previous[0]
                if previous and (
                    (
                        new_stream
                        and (
                            batch["stream_id"] in old_streams
                            or batch["received_at_ns"] < previous[2]
                        )
                    )
                    or (not new_stream and batch["sequence"] <= previous[1])
                ):
                    with self.lock:
                        self.status["stale_total"] += 1
                    continue
                if previous and new_stream:
                    old_streams.append(previous[0])
                self.state("building")
                output = self.builder.build(batch)
                if self.journal is not None and output["rows"]:
                    self.state("buffer_wait")
                    if not self.journal.append(output, self.stop):
                        break
                    self.state("building")
                publisher.publish(output)
                sessions[key] = (
                    batch["stream_id"],
                    batch["sequence"],
                    batch["received_at_ns"],
                )
                metadata = output["row_builder"]
                with self.lock:
                    self.status["processed"] += 1
                    self.status["sent_total"] += 1
                    self.status["rows_total"] += len(output["rows"])
                    for field in (
                        "detection_rows",
                        "notification_rows",
                        "node_rows",
                        "skipped_disabled",
                        "skipped_no_table",
                    ):
                        self.status[field + "_total"] += metadata[field]
                    self.status["last_build_ms"] = metadata["build_ms"]
                    self.status["processed_at_ns"] = time.time_ns()
                log.debug(
                    "row batch sent",
                    extra={
                        "stage": "output",
                        "event": "rows_sent",
                        "fields": {
                            "source_id": identity,
                            "sequence": batch["sequence"],
                            "source_type": batch["message_type"],
                            "rows": len(output["rows"]),
                        },
                    },
                )
                self.state("waiting")
                if self.max_frames and self.snapshot()["processed"] >= self.max_frames:
                    break
        except Exception as exc:
            self.error = exc
            log.exception("row builder failed")
        finally:
            self.state("closing")
            if publisher:
                publisher.close()
            self.state("failed" if self.error else "stopped")
            self.stop.set()
