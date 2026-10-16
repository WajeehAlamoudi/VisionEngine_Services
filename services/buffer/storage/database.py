"""Single-owner persistent row buffer, idempotent writes and leased ingest batches."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from ..messaging.protocol import BufferError
from ..messaging.rows import encode as encode_rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    batch_id TEXT PRIMARY KEY, digest TEXT NOT NULL, row_count INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    table_name TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','inflight','sent')),
    created_at REAL NOT NULL, sent_at REAL, lease_id TEXT, lease_until REAL,
    UNIQUE(batch_id, ordinal)
);
CREATE INDEX IF NOT EXISTS rows_status_id ON rows(status,id);
CREATE INDEX IF NOT EXISTS rows_batch ON rows(batch_id);
CREATE TABLE IF NOT EXISTS leases (
    token TEXT PRIMARY KEY, state TEXT NOT NULL, expires_at REAL NOT NULL,
    ids TEXT NOT NULL, acked TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def integer(value, name, minimum=1, maximum=1000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BufferError("invalid", f"invalid {name}")
    return value


class Database:
    def __init__(self, path, *, max_size_mb=512, delete_after_hours=24, receipt_days=7):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_size_mb * 1024 * 1024
        self.retention = delete_after_hours * 3600
        self.receipt_retention = receipt_days * 86400
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            legacy = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='buffer'"
            ).fetchone()
            if version not in (0, 1) or legacy:
                raise ValueError(
                    "unsupported or legacy buffer database; explicit migration is required"
                )
            self.connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=10000")
            self.connection.executescript(SCHEMA)
            self.connection.execute("PRAGMA user_version=1")
            self.connection.commit()
            self.maintain()
        except Exception:
            self.connection.close()
            raise

    def _count(self, name, amount=1):
        self.connection.execute(
            "INSERT INTO counters(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
            (name, amount),
        )

    def _expire(self):
        now = time.time()
        self.connection.execute(
            "UPDATE rows SET status='pending',lease_id=NULL,lease_until=NULL WHERE status='inflight' AND lease_until<=?",
            (now,),
        )
        self.connection.execute(
            "UPDATE leases SET state='expired' WHERE state='active' AND expires_at<=?",
            (now,),
        )

    def put(self, batch_id, batch):
        if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 128:
            raise BufferError("invalid", "invalid batch ID")
        encode_rows(batch)
        if not batch["rows"]:
            return {"batch_id": batch_id, "stored": 0, "duplicate": False}
        payload = json.dumps(
            batch["rows"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(payload.encode()) > self.max_bytes // 2:
            raise BufferError(
                "too_large",
                "batch exceeds half the configured buffer capacity; split it or increase capacity",
            )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection:
            previous = self.connection.execute(
                "SELECT digest,row_count FROM receipts WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise BufferError(
                        "conflict", "batch ID was already committed with different rows"
                    )
                self._count("duplicates_total")
                return {
                    "batch_id": batch_id,
                    "stored": previous["row_count"],
                    "duplicate": True,
                }
            # If only live metadata/leased rows fill capacity, stop admitting more.
            page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
            live_pages = (
                self.connection.execute("PRAGMA page_count").fetchone()[0]
                - self.connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            evictable = self.connection.execute(
                "SELECT 1 FROM rows WHERE status IN ('pending','sent') LIMIT 1"
            ).fetchone()
            if live_pages * page_size >= self.max_bytes and not evictable:
                raise BufferError(
                    "capacity",
                    "buffer metadata or leased rows fill capacity; retry after cleanup",
                )
            now = time.time()
            self.connection.execute(
                "INSERT INTO receipts VALUES (?,?,?,?)",
                (batch_id, digest, len(batch["rows"]), now),
            )
            self.connection.executemany(
                "INSERT INTO rows(batch_id,ordinal,table_name,payload,status,created_at) VALUES (?,?,?,?,'pending',?)",
                [
                    (
                        batch_id,
                        index,
                        item["table"],
                        json.dumps(item["row"], separators=(",", ":"), allow_nan=False),
                        now,
                    )
                    for index, item in enumerate(batch["rows"])
                ],
            )
            self._count("batches_total")
            self._count("received_rows_total", len(batch["rows"]))
        # The receipt and rows are committed together before acknowledging.
        self.maintain()
        return {"batch_id": batch_id, "stored": len(batch["rows"]), "duplicate": False}

    def _select(self, limit, max_bytes):
        integer(limit, "limit")
        integer(max_bytes, "max_bytes", 1024, 16 * 1024 * 1024)
        selected, size = [], 0
        for record in self.connection.execute(
            "SELECT id,table_name,payload,created_at FROM rows WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        ):
            item = {
                "id": record["id"],
                "table": record["table_name"],
                "row": json.loads(record["payload"]),
                "created_at": record["created_at"],
            }
            item_size = len(
                json.dumps(item, separators=(",", ":"), allow_nan=False).encode()
            )
            if size + item_size > max_bytes:
                if not selected:
                    raise BufferError(
                        "too_large", "oldest row exceeds requested max_bytes"
                    )
                break
            selected.append(item)
            size += item_size
        return selected

    def peek(self, limit=50, max_bytes=8 * 1024 * 1024):
        with self.connection:
            self._expire()
            return {"rows": self._select(limit, max_bytes)}

    def claim(self, limit=50, lease_seconds=60, max_bytes=8 * 1024 * 1024):
        integer(lease_seconds, "lease_seconds", 1, 3600)
        with self.connection:
            self._expire()
            selected = self._select(limit, max_bytes)
            if not selected:
                return {
                    "message_type": "buffer.batch.v1",
                    "lease_id": None,
                    "expires_at": None,
                    "rows": [],
                }
            token, now = uuid.uuid4().hex, time.time()
            ids = [item["id"] for item in selected]
            self.connection.execute(
                "INSERT INTO leases VALUES (?,'active',?,?,?,?)",
                (token, now + lease_seconds, json.dumps(ids), "[]", now),
            )
            self.connection.executemany(
                "UPDATE rows SET status='inflight',lease_id=?,lease_until=? WHERE id=?",
                [(token, now + lease_seconds, identity) for identity in ids],
            )
        return {
            "message_type": "buffer.batch.v1",
            "lease_id": token,
            "expires_at": now + lease_seconds,
            "rows": selected,
        }

    def ack(self, lease_id, ids=None):
        with self.connection:
            self._expire()
            lease = self.connection.execute(
                "SELECT * FROM leases WHERE token=?", (lease_id,)
            ).fetchone()
            if lease is None:
                raise BufferError("invalid_lease", "unknown lease")
            members, acknowledged = (
                set(json.loads(lease["ids"])),
                set(json.loads(lease["acked"])),
            )
            if ids is not None and (
                not isinstance(ids, list)
                or any(type(i) is not int for i in ids)
                or len(ids) != len(set(ids))
            ):
                raise BufferError("invalid", "ack IDs must be unique integers")
            requested = members if ids is None else set(ids)
            if not requested <= members:
                raise BufferError(
                    "invalid_lease", "ack contains rows outside this lease"
                )
            if requested <= acknowledged:
                return {"acked": len(requested), "duplicate": True}
            if lease["state"] != "active" or lease["expires_at"] <= time.time():
                raise BufferError(
                    "lease_expired",
                    "lease expired or was released; rows may be claimed again",
                )
            fresh = requested - acknowledged
            self.connection.executemany(
                "UPDATE rows SET status='sent',sent_at=? WHERE id=? AND status='inflight' AND lease_id=?",
                [(time.time(), identity, lease_id) for identity in fresh],
            )
            acknowledged |= fresh
            self.connection.execute(
                "UPDATE leases SET acked=?,state=? WHERE token=?",
                (
                    json.dumps(sorted(acknowledged)),
                    "acked" if acknowledged == members else "active",
                    lease_id,
                ),
            )
            self._count("sent_rows_total", len(fresh))
        self.maintain()
        return {"acked": len(requested), "duplicate": False}

    def renew(self, lease_id, lease_seconds=60):
        integer(lease_seconds, "lease_seconds", 1, 3600)
        with self.connection:
            lease = self.connection.execute(
                "SELECT state,expires_at FROM leases WHERE token=?", (lease_id,)
            ).fetchone()
            if (
                lease is None
                or lease["state"] != "active"
                or lease["expires_at"] <= time.time()
            ):
                raise BufferError(
                    "lease_expired", "cannot renew an expired or completed lease"
                )
            expires = time.time() + lease_seconds
            self.connection.execute(
                "UPDATE leases SET expires_at=? WHERE token=?", (expires, lease_id)
            )
            self.connection.execute(
                "UPDATE rows SET lease_until=? WHERE lease_id=? AND status='inflight'",
                (expires, lease_id),
            )
        return {"lease_id": lease_id, "expires_at": expires}

    def release(self, lease_id):
        with self.connection:
            lease = self.connection.execute(
                "SELECT state FROM leases WHERE token=?", (lease_id,)
            ).fetchone()
            if lease is None:
                raise BufferError("invalid_lease", "unknown lease")
            result = self.connection.execute(
                "UPDATE rows SET status='pending',lease_id=NULL,lease_until=NULL WHERE status='inflight' AND lease_id=?",
                (lease_id,),
            )
            self.connection.execute(
                "UPDATE leases SET state='released' WHERE token=? AND state='active'",
                (lease_id,),
            )
            return {"released": result.rowcount}

    def disk_bytes(self):
        return sum(
            path.stat().st_size
            for path in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            )
            if path.exists()
        )

    def _reclaim(self):
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for _ in range(1024):
            if self.connection.execute("PRAGMA freelist_count").fetchone()[0] == 0:
                break
            self.connection.execute("PRAGMA incremental_vacuum(1)")
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def maintain(self):
        now = time.time()
        with self.connection:
            self._expire()
            deleted = self.connection.execute(
                "DELETE FROM rows WHERE status='sent' AND sent_at<=?",
                (now - self.retention,),
            ).rowcount
            self._count("purged_sent_total", deleted)
            self.connection.execute(
                "DELETE FROM receipts WHERE created_at<? AND NOT EXISTS (SELECT 1 FROM rows WHERE rows.batch_id=receipts.batch_id)",
                (now - self.receipt_retention,),
            )
            self.connection.execute(
                "DELETE FROM leases WHERE state!='active' AND created_at<?",
                (now - self.receipt_retention,),
            )
        if self.disk_bytes() <= self.max_bytes:
            return
        self._reclaim()
        while self.disk_bytes() > self.max_bytes:
            selected = self.connection.execute(
                "SELECT id,status FROM rows WHERE status='sent' ORDER BY id LIMIT 1000"
            ).fetchall()
            if not selected:
                selected = self.connection.execute(
                    "SELECT id,status FROM rows WHERE status='pending' ORDER BY id LIMIT 1000"
                ).fetchall()
            if not selected:
                break  # Leases and retained dedup receipts cannot be discarded safely.
            with self.connection:
                self.connection.executemany(
                    "DELETE FROM rows WHERE id=?", [(row["id"],) for row in selected]
                )
                self._count(
                    "dropped_pending_total",
                    sum(row["status"] == "pending" for row in selected),
                )
                self._count(
                    "purged_sent_total",
                    sum(row["status"] == "sent" for row in selected),
                )
            self._reclaim()

    def stats(self):
        with self.connection:
            self._expire()
            counts = dict(
                self.connection.execute(
                    "SELECT status,COUNT(*) FROM rows GROUP BY status"
                )
            )
            counters = dict(self.connection.execute("SELECT name,value FROM counters"))
        disk = self.disk_bytes()
        return {
            "pending": counts.get("pending", 0) + counts.get("inflight", 0),
            "inflight": counts.get("inflight", 0),
            "sent": counts.get("sent", 0),
            "size_mb": round(disk / 1024 / 1024, 3),
            "disk_bytes": disk,
            "max_size_mb": self.max_bytes / 1024 / 1024,
            "over_limit": disk > self.max_bytes,
            "receipts": self.connection.execute(
                "SELECT COUNT(*) FROM receipts"
            ).fetchone()[0],
            **{
                key: counters.get(key, 0)
                for key in (
                    "batches_total",
                    "received_rows_total",
                    "duplicates_total",
                    "sent_rows_total",
                    "dropped_pending_total",
                    "purged_sent_total",
                )
            },
        }

    def close(self):
        self.connection.close()
