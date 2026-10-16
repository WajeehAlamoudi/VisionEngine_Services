"""Single dataset owner; all disk I/O is isolated from camera/result reception."""

import logging
from pathlib import Path
import sqlite3
import threading
import time

from .filters import apply_filters, in_schedule, Sampler
from ..storage.dataset import Dataset

log = logging.getLogger(__name__)


class CollectorWorker(threading.Thread):
    def __init__(
        self,
        collection,
        session,
        camera,
        model,
        inbox,
        stop,
        *,
        output_dir=None,
        min_free_bytes=268435456,
        max_batches=None,
    ):
        super().__init__(name="collector-worker", daemon=True)
        self.collection, self.session, self.camera, self.model = (
            collection,
            session,
            camera,
            model,
        )
        self.inbox, self.stop = inbox, stop
        self.output_dir = Path(output_dir or collection.output_dir)
        self.min_free_bytes, self.max_batches = min_free_bytes, max_batches
        self.error = None
        self.lock = threading.Lock()
        self.status = {
            "state": "loading",
            "since": time.monotonic(),
            "heartbeat_at_ns": time.time_ns(),
            "processed_at_ns": time.time_ns(),
            "processed": 0,
            "session_id": session.id,
            "camera_id": camera.id,
            "saved_total": 0,
            "saved_this_run": 0,
            "failed_total": 0,
            "rejected_total": 0,
            "skipped_schedule": 0,
            "skipped_filters": 0,
            "skipped_sampling": 0,
            "duplicate_saved_total": 0,
            "labels_total": 0,
            "last_save_ms": 0,
            "dependency_error": None,
            "recovered_total": 0,
            "quarantined_total": 0,
        }

    def update(self, **values):
        with self.lock:
            self.status.update(values)
            self.status["heartbeat_at_ns"] = time.time_ns()
            self.status["processed_at_ns"] = time.time_ns()

    def state(self, value):
        self.update(state=value, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def run(self):
        dataset = None
        next_error = 0
        try:
            dataset = Dataset(
                self.output_dir,
                self.session.id,
                self.camera.id,
                self.model.classes,
                metadata=self.session.save.metadata,
                min_free_bytes=self.min_free_bytes,
            )
            sampler = Sampler(self.session.sampling)
            self.update(
                saved_total=dataset.total,
                recovered_total=dataset.recovered_total,
                quarantined_total=dataset.quarantined_total,
            )
            self.state("waiting")
            while not self.stop.is_set():
                if self.session.max_frames and dataset.total >= self.session.max_frames:
                    self.state("completed")
                    self.inbox.clear()
                    self.stop.wait(0.1)
                    continue
                item = self.inbox.pop()
                if item is None:
                    self.update()
                    self.stop.wait(0.02)
                    continue
                header, image, result = item
                status = self.snapshot()
                self.update(
                    processed=status["processed"] + 1, last_matched_at_ns=time.time_ns()
                )
                if not in_schedule(
                    self.session.schedule,
                    header["received_at_ns"],
                ):
                    self.update(
                        skipped_schedule=self.snapshot()["skipped_schedule"] + 1
                    )
                else:
                    qualifying = apply_filters(
                        result["detections"], self.session.filters
                    )
                    if len(qualifying) < self.session.filters.min_detections:
                        self.update(
                            skipped_filters=self.snapshot()["skipped_filters"] + 1
                        )
                    elif not sampler.accepts(qualifying):
                        self.update(
                            skipped_sampling=self.snapshot()["skipped_sampling"] + 1
                        )
                    else:
                        self.state("saving")
                        started = time.monotonic()
                        try:
                            written = dataset.save(
                                header,
                                image,
                                result,
                                qualifying,
                                max_frames=self.session.max_frames,
                            )
                            self.update(
                                saved_total=dataset.total,
                                recovered_total=dataset.recovered_total,
                                quarantined_total=dataset.quarantined_total,
                                dependency_error=None,
                            )
                            if written:
                                sampler.saved()
                                self.update(
                                    saved_this_run=self.snapshot()["saved_this_run"]
                                    + 1,
                                    labels_total=self.snapshot()["labels_total"]
                                    + len(qualifying),
                                    last_save_ms=(time.monotonic() - started) * 1000,
                                )
                                log.info(
                                    "dataset sample saved",
                                    extra={
                                        "stage": "output",
                                        "event": "sample_saved",
                                        "fields": {
                                            "session_id": self.session.id,
                                            "camera_id": self.camera.id,
                                            "sequence": header["sequence"],
                                            "saved": dataset.total,
                                            "labels": len(qualifying),
                                        },
                                    },
                                )
                            elif (
                                not self.session.max_frames
                                or dataset.total < self.session.max_frames
                            ):
                                self.update(
                                    duplicate_saved_total=self.snapshot()[
                                        "duplicate_saved_total"
                                    ]
                                    + 1
                                )
                            self.state("waiting")
                        except (OSError, sqlite3.Error):
                            self.update(
                                failed_total=self.snapshot()["failed_total"] + 1,
                                dependency_error="collector_storage_error",
                            )
                            self.state("storage_error")
                            if time.monotonic() >= next_error:
                                log.warning(
                                    "dataset write failed; existing samples preserved",
                                    extra={"event": "storage_error"},
                                )
                                next_error = time.monotonic() + 5
                            self.stop.wait(1)
                        except ValueError:
                            self.update(
                                rejected_total=self.snapshot()["rejected_total"] + 1
                            )
                            self.state("waiting")
                            log.warning(
                                "dataset sample rejected",
                                extra={"event": "sample_rejected"},
                            )
                if (
                    self.max_batches
                    and self.snapshot()["processed"] >= self.max_batches
                ):
                    break
        except Exception as exc:
            self.error = exc
            log.error(
                "collector worker failed (%s): %s", type(exc).__name__, str(exc)[:250]
            )
        finally:
            if dataset:
                dataset.close()
            self.state("failed" if self.error else "stopped")
            self.stop.set()
