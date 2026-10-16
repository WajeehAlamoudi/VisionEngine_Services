"""Bounded input queue and serial enrichment for one camera."""

from collections import deque
import logging
import threading
import time

from .enricher import enrich
from ..messaging.publisher import Publisher

log = logging.getLogger(__name__)


class Inbox:
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


class EnrichmentWorker(threading.Thread):
    def __init__(self, camera, inbox, stop, bind, *, max_frames=None):
        super().__init__(name="enrichment-worker", daemon=True)
        self.camera, self.inbox, self.stop, self.bind = camera, inbox, stop, bind
        self.max_frames, self.error = max_frames, None
        self.lock = threading.Lock()
        self.status = {
            "state": "loading",
            "since": time.monotonic(),
            "processed": 0,
            "processed_at_ns": 0,
            "sent_total": 0,
            "stale_total": 0,
            "events_total": 0,
            "last_enrichment_ms": 0,
        }

    def state(self, value, **fields):
        with self.lock:
            self.status.update(state=value, since=time.monotonic(), **fields)

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def run(self):
        publisher = None
        stream, sequence, timestamp = None, 0, 0
        retired = deque(maxlen=64)
        try:
            publisher = Publisher(self.bind)
            log.info(
                "enrichment ready",
                extra={"event": "service_ready", "fields": {"bind": self.bind}},
            )
            self.state("waiting")
            while not self.stop.is_set():
                batch = self.inbox.pop()
                if batch is None:
                    self.stop.wait(0.01)
                    continue
                new_stream = batch["stream_id"] != stream
                if (
                    new_stream
                    and (
                        batch["stream_id"] in retired
                        or batch["received_at_ns"] < timestamp
                    )
                ) or (not new_stream and batch["sequence"] <= sequence):
                    with self.lock:
                        self.status["stale_total"] += 1
                    continue
                if new_stream and stream is not None:
                    retired.append(stream)
                stream, sequence, timestamp = (
                    batch["stream_id"],
                    batch["sequence"],
                    batch["received_at_ns"],
                )
                self.state("enriching")
                output = enrich(batch, self.camera)
                publisher.publish(output)
                with self.lock:
                    self.status["processed"] += 1
                    self.status["sent_total"] += 1
                    self.status["events_total"] += len(output["events"])
                    self.status["processed_at_ns"] = time.time_ns()
                    self.status["last_enrichment_ms"] = output["enrichment"][
                        "enrichment_ms"
                    ]
                log.debug(
                    "events sent",
                    extra={
                        "stage": "output",
                        "event": "events_sent",
                        "fields": {
                            "stream_id": stream,
                            "sequence": sequence,
                            "events": len(output["events"]),
                        },
                    },
                )
                self.state("waiting")
                if self.max_frames and self.snapshot()["processed"] >= self.max_frames:
                    break
        except Exception as exc:
            self.error = exc
            log.exception("enrichment worker failed")
        finally:
            self.state("closing")
            if publisher:
                publisher.close()
            self.state("failed" if self.error else "stopped")
            self.stop.set()
