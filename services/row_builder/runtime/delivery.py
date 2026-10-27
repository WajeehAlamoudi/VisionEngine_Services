"""Persistent producer journal and retrying commit acknowledgements to buffer."""

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid

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
        self.ready = threading.Condition(self.lock)
        self.max_items, self.max_bytes = max_items, max_bytes
        self.connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=10
        )
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA journal_size_limit=1048576")
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,bytes INTEGER NOT NULL)"
        )
        # A new producer epoch must never reuse a retired sequence. Old development
        # journals require explicit reset; never silently discard pending work.
        columns = {r[1] for r in self.connection.execute("PRAGMA table_info(pending)")}
        if "digest" not in columns:
            if self.connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0]:
                self.connection.close()
                raise ValueError(
                    "legacy journal has pending work; explicit reset required"
                )
            self.connection.execute("ALTER TABLE pending ADD COLUMN digest TEXT")
            self.connection.execute("ALTER TABLE pending ADD COLUMN source_key TEXT")
            self.connection.execute(
                "CREATE UNIQUE INDEX pending_source ON pending(source_key)"
            )
            self.connection.execute("DELETE FROM sqlite_sequence WHERE name='pending'")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS producer (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "producer_id TEXT NOT NULL, retired_through INTEGER NOT NULL)"
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO producer VALUES (1,?,0)", (str(uuid.uuid4()),)
        )
        self.connection.commit()

    def checkpoint(self):
        with self.lock:
            row = self.connection.execute(
                "SELECT producer_id,retired_through FROM producer WHERE singleton=1"
            ).fetchone()
            return {"producer_id": row[0], "retired_through": row[1]}

    def append(self, batch, stop):
        encode(
            batch
        )  # Check the same bounded contract used by the inspection publisher.
        source_key = batch_id(batch)
        identity = str(uuid.uuid4())
        payload = json.dumps(batch, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(
            json.dumps(
                batch["rows"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        size = len(payload.encode())
        if size > self.max_bytes:
            raise ValueError("row batch exceeds delivery journal byte capacity")
        while not stop.is_set():
            with self.ready:
                if self.connection.execute(
                    "SELECT 1 FROM pending WHERE source_key=?", (source_key,)
                ).fetchone():
                    return True
                count, total = self.connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM pending"
                ).fetchone()
                if count < self.max_items and total + size <= self.max_bytes:
                    with self.connection:
                        self.connection.execute(
                            "INSERT INTO pending(batch_id,payload,bytes,digest,source_key) VALUES (?,?,?,?,?)",
                            (identity, payload, size, digest, source_key),
                        )
                    self.ready.notify_all()
                    return True
                # ACKs notify space availability; timeout only checks shutdown.
                self.ready.wait(0.1)
        return False

    def first(self, stop=None):
        with self.ready:
            while stop is None or not stop.is_set():
                row = self.connection.execute(
                    "SELECT batch_id,payload,id,digest FROM pending ORDER BY id LIMIT 1"
                ).fetchone()
                if row:
                    return row[0], json.loads(row[1]), row[2], row[3]
                if stop is None:
                    return None
                # Appends notify after commit; timeout only checks shutdown.
                self.ready.wait(0.1)
        return None

    def acknowledged(self, identity):
        with self.ready:
            with self.connection:
                first = self.connection.execute(
                    "SELECT id,batch_id FROM pending ORDER BY id LIMIT 1"
                ).fetchone()
                if first is None or first[1] != identity:
                    raise ValueError("journal ACK is not the oldest pending batch")
                self.connection.execute("DELETE FROM pending WHERE id=?", (first[0],))
                self.connection.execute(
                    "UPDATE producer SET retired_through=? WHERE singleton=1",
                    (first[0],),
                )
            self.ready.notify_all()

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
            with self.lock:
                self.status["state"] = "waiting"
            reported = -1
            identity = None
            while not self.stop.is_set():
                item = self.journal.first()
                checkpoint = self.journal.checkpoint()
                try:
                    if item is None:
                        if checkpoint["retired_through"] > reported:
                            result = client.request("retire", **checkpoint)
                            if (
                                result.get("retired_through")
                                != checkpoint["retired_through"]
                            ):
                                raise ValueError("invalid retirement acknowledgement")
                            reported = checkpoint["retired_through"]
                        item = self.journal.first(self.stop)
                        if item is None:
                            continue
                    identity, batch, sequence, digest = item
                    result = client.request(
                        "put",
                        batch_id=identity,
                        batch=batch,
                        sequence=sequence,
                        digest=digest,
                        **checkpoint,
                    )
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
