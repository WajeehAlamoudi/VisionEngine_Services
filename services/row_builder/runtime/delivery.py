"""Persistent producer journal and retrying commit acknowledgements to buffer."""

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from ..messaging.buffer import Client, BufferError
from ..messaging.rows import encode

log = logging.getLogger(__name__)


def batch_id(batch):
    identity = {
        key: batch.get(key)
        for key in ("source_type", "camera_id", "device_id", "stream_id", "sequence")
    }
    identity["rows"] = batch["rows"]
    return hashlib.sha256(
        json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class Journal:
    def __init__(self, path, max_items=1000, max_bytes=4_194_304):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.max_items, self.max_bytes = max_items, max_bytes
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=10
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA journal_size_limit=1048576")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,bytes INTEGER NOT NULL)"
        )
        self.connection.commit()

    def append(self, batch, stop):
        encode(
            batch
        )  # Check the same bounded contract used by the inspection publisher.
        identity = batch_id(batch)
        payload = json.dumps(batch, separators=(",", ":"), allow_nan=False)
        size = len(payload.encode())
        if size > self.max_bytes:
            raise ValueError("row batch exceeds delivery journal byte capacity")
        while not stop.is_set():
            with self.lock:
                if self.connection.execute(
                    "SELECT 1 FROM pending WHERE batch_id=?", (identity,)
                ).fetchone():
                    return True
                count, total = self.connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM pending"
                ).fetchone()
                if count < self.max_items and total + size <= self.max_bytes:
                    with self.connection:
                        self.connection.execute(
                            "INSERT INTO pending(batch_id,payload,bytes) VALUES (?,?,?)",
                            (identity, payload, size),
                        )
                    return True
            stop.wait(0.05)
        return False

    def first(self):
        with self.lock:
            row = self.connection.execute(
                "SELECT batch_id,payload FROM pending ORDER BY id LIMIT 1"
            ).fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def acknowledged(self, identity):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM pending WHERE batch_id=?", (identity,))

    def snapshot(self):
        with self.lock:
            count, size = self.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM pending"
            ).fetchone()
            return {"pending_batches": count, "pending_bytes": size}

    def close(self):
        with self.lock:
            self.connection.close()


class Delivery(threading.Thread):
    def __init__(self, journal, endpoint, stop, *, timeout_ms=2000, retry_seconds=1):
        super().__init__(name="row-buffer-delivery", daemon=True)
        self.journal, self.endpoint, self.stop = journal, endpoint, stop
        self.timeout_ms, self.retry_seconds = timeout_ms, retry_seconds
        self.error = None
        self.lock = threading.Lock()
        self.status = {
            "state": "starting",
            "acked_batches": 0,
            "retries": 0,
            "last_ack_at_ns": 0,
        }

    def snapshot(self):
        with self.lock:
            return {**self.status, **self.journal.snapshot()}

    def run(self):
        client = Client(self.endpoint, self.timeout_ms)
        try:
            while not self.stop.is_set():
                item = self.journal.first()
                if item is None:
                    with self.lock:
                        self.status["state"] = "waiting"
                    self.stop.wait(0.05)
                    continue
                identity, batch = item
                try:
                    result = client.request("put", batch_id=identity, batch=batch)
                    if result.get("batch_id") != identity or result.get(
                        "stored"
                    ) != len(batch["rows"]):
                        raise ValueError("invalid buffer commit acknowledgement")
                    self.journal.acknowledged(identity)
                    with self.lock:
                        self.status["state"] = "waiting"
                        self.status["acked_batches"] += 1
                        self.status["last_ack_at_ns"] = time.time_ns()
                except (TimeoutError, BufferError) as exc:
                    if isinstance(exc, BufferError) and exc.code not in (
                        "storage",
                        "capacity",
                    ):
                        raise
                    with self.lock:
                        self.status["state"] = "retrying"
                        self.status["retries"] += 1
                    log.warning(
                        "buffer delivery retry; batch remains journaled",
                        extra={
                            "stage": "output",
                            "event": "buffer_retry",
                            "fields": {"batch_id": identity},
                        },
                    )
                    self.stop.wait(self.retry_seconds)
        except Exception as exc:
            self.error = exc
            log.exception("buffer delivery failed; pending batches retained")
            self.stop.set()
        finally:
            client.close()
