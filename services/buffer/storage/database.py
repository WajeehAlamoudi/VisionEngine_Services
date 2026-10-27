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
-- Storage: rows. Producer coordination: checkpoints + only unretired handoffs.
CREATE TABLE IF NOT EXISTS producers (
    producer_id TEXT PRIMARY KEY, saved_through INTEGER NOT NULL,
    retired_through INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_batches (
    batch_id TEXT PRIMARY KEY, producer_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    digest TEXT NOT NULL, row_count INTEGER NOT NULL,
    UNIQUE(producer_id, sequence)
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
CREATE INDEX IF NOT EXISTS rows_pending_table ON rows(status,table_name,id);
CREATE INDEX IF NOT EXISTS rows_lease ON rows(lease_id);
CREATE INDEX IF NOT EXISTS rows_lease_expiry ON rows(lease_until) WHERE status='inflight';
CREATE INDEX IF NOT EXISTS rows_status_sent_at ON rows(status,sent_at);
CREATE TABLE IF NOT EXISTS leases (
    token TEXT PRIMARY KEY, state TEXT NOT NULL, expires_at REAL NOT NULL,
    ids TEXT NOT NULL, acked TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS leases_active_expiry ON leases(expires_at) WHERE state='active';
CREATE INDEX IF NOT EXISTS leases_created_at ON leases(created_at);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, row_ids TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','completed')),
    created_at REAL NOT NULL, completed_at REAL
);
CREATE INDEX IF NOT EXISTS deliveries_state ON deliveries(state,created_at);
CREATE INDEX IF NOT EXISTS deliveries_state_completed_at ON deliveries(state,completed_at);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


# Pending deliveries own their rows even between leases and across restarts.
UNASSIGNED = """NOT EXISTS (
    SELECT 1 FROM deliveries AS d, json_each(d.row_ids) AS member
    WHERE d.state='pending' AND member.value=rows.id
)"""


def integer(value, name, minimum=1, maximum=1000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise BufferError("invalid", f"invalid {name}")
    return value


class Database:
    def __init__(
        self, path, *, max_size_mb=512, delete_after_hours=24, initialize=True
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_size_mb * 1024 * 1024
        # Delivered payloads are removed after backend ACK. The old YAML retention
        # remains accepted for configuration compatibility, not receipt history.
        self.retention = delete_after_hours * 3600
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            legacy = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='buffer'"
            ).fetchone()
            existing = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='rows'"
            ).fetchone()
            if version not in (0, 3) or legacy or (version == 0 and existing):
                raise ValueError(
                    "unsupported or legacy buffer database; explicit migration is required"
                )
            self.connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA busy_timeout=10000")
            self.connection.execute("PRAGMA wal_autocheckpoint=128")
            self.connection.execute("PRAGMA journal_size_limit=1048576")
            # A page ceiling limits the main database, not transient WAL/SHM bytes.
            requested_pages = int(
                self.max_bytes
                // self.connection.execute("PRAGMA page_size").fetchone()[0]
            )
            actual_pages = self.connection.execute(
                "PRAGMA max_page_count=" + str(requested_pages)
            ).fetchone()[0]
            if actual_pages > requested_pages:
                raise ValueError(
                    "existing database exceeds configured page ceiling; explicit reclamation/reset required"
                )
            if initialize:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=3;\nCOMMIT;"
                )
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
            "DELETE FROM leases WHERE state='active' AND expires_at<=?", (now,)
        )

    @staticmethod
    def _producer_id(value):
        try:
            parsed = uuid.UUID(value)
            if str(parsed) != value or parsed.version != 4:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise BufferError("invalid", "expected canonical producer UUIDv4")

    def _retire(self, producer_id, retired_through):
        self._producer_id(producer_id)
        integer(retired_through, "retired_through", 0, 2**63 - 1)
        self.connection.execute(
            "INSERT OR IGNORE INTO producers VALUES (?,0,0)", (producer_id,)
        )
        producer = self.connection.execute(
            "SELECT * FROM producers WHERE producer_id=?", (producer_id,)
        ).fetchone()
        if retired_through > producer["saved_through"]:
            raise BufferError("conflict", "cannot retire batches not durably saved")
        if retired_through > producer["retired_through"]:
            self.connection.execute(
                "UPDATE producers SET retired_through=? WHERE producer_id=?",
                (retired_through, producer_id),
            )
            self.connection.execute(
                "DELETE FROM source_batches WHERE producer_id=? AND sequence<=?",
                (producer_id, retired_through),
            )
        return max(retired_through, producer["retired_through"])

    def retire(self, producer_id, retired_through):
        with self.connection:
            value = self._retire(producer_id, retired_through)
        return {"retired_through": value}

    def put(self, batch_id, batch, producer_id, sequence, digest, retired_through=0):
        self._producer_id(batch_id)
        integer(sequence, "sequence", 1, 2**63 - 1)
        encode_rows(batch)
        payload = json.dumps(
            batch["rows"], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        actual = hashlib.sha256(payload.encode()).hexdigest()
        if actual != digest:
            raise BufferError("conflict", "payload hash does not match rows")
        if len(payload.encode()) > self.max_bytes // 2:
            raise BufferError(
                "too_large", "batch exceeds half the configured buffer capacity"
            )
        try:
            with self.connection:
                # Reserve the write lock before reading coordination state.
                self.connection.execute(
                    "UPDATE counters SET value=value WHERE name='batches_total'"
                )
                retired = self._retire(producer_id, retired_through)
                if sequence <= retired:
                    return {
                        "batch_id": batch_id,
                        "stored": len(batch["rows"]),
                        "duplicate": True,
                        "retired": True,
                    }
                previous = self.connection.execute(
                    "SELECT * FROM source_batches WHERE batch_id=? OR (producer_id=? AND sequence=?)",
                    (batch_id, producer_id, sequence),
                ).fetchone()
                if previous:
                    if (
                        previous["batch_id"],
                        previous["producer_id"],
                        previous["sequence"],
                        previous["digest"],
                        previous["row_count"],
                    ) != (batch_id, producer_id, sequence, digest, len(batch["rows"])):
                        raise BufferError(
                            "conflict",
                            "batch identity was reused with different content",
                        )
                    self._count("duplicates_total")
                    return {
                        "batch_id": batch_id,
                        "stored": previous["row_count"],
                        "duplicate": True,
                    }
                saved = self.connection.execute(
                    "SELECT saved_through FROM producers WHERE producer_id=?",
                    (producer_id,),
                ).fetchone()[0]
                if sequence != saved + 1:
                    raise BufferError(
                        "conflict", "producer sequence must be contiguous"
                    )
                # Reserve headroom for claims/ACKs/checkpoints to drain a full buffer.
                page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
                live = (
                    self.connection.execute("PRAGMA page_count").fetchone()[0]
                    - self.connection.execute("PRAGMA freelist_count").fetchone()[0]
                ) * page_size
                if live + len(payload.encode()) >= self.max_bytes * 0.85:
                    raise BufferError(
                        "capacity", "live buffer capacity reached; retry after delivery"
                    )
                self.connection.execute(
                    "INSERT INTO source_batches VALUES (?,?,?,?,?)",
                    (batch_id, producer_id, sequence, digest, len(batch["rows"])),
                )
                now = time.time()
                self.connection.executemany(
                    "INSERT INTO rows(batch_id,ordinal,table_name,payload,status,created_at) "
                    "VALUES (?,?,?,?,'pending',?)",
                    (
                        (
                            batch_id,
                            index,
                            item["table"],
                            json.dumps(
                                item["row"], separators=(",", ":"), allow_nan=False
                            ),
                            now,
                        )
                        for index, item in enumerate(batch["rows"])
                    ),
                )
                self.connection.execute(
                    "UPDATE producers SET saved_through=? WHERE producer_id=?",
                    (sequence, producer_id),
                )
                self._count("batches_total")
                self._count("received_rows_total", len(batch["rows"]))
                live = (
                    self.connection.execute("PRAGMA page_count").fetchone()[0]
                    - self.connection.execute("PRAGMA freelist_count").fetchone()[0]
                ) * page_size
                if live > self.max_bytes * 0.90:
                    raise BufferError(
                        "capacity", "batch would consume delivery recovery headroom"
                    )
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise BufferError("capacity", "SQLite page capacity reached") from exc
            raise
        return {"batch_id": batch_id, "stored": len(batch["rows"]), "duplicate": False}

    def _select(self, limit, max_bytes, *, table_name=None, unassigned=True):
        integer(limit, "limit")
        integer(max_bytes, "max_bytes", 1024, 16 * 1024 * 1024)
        selected, size = [], 0
        conditions, arguments = ["status='pending'"], []
        if unassigned:
            conditions.append(UNASSIGNED)
        if table_name is not None:
            conditions.append("table_name=?")
            arguments.append(table_name)
        for record in self.connection.execute(
            "SELECT id,table_name,payload,created_at FROM rows WHERE "
            + " AND ".join(conditions)
            + " ORDER BY id LIMIT ?",
            (*arguments, limit),
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
            return {"rows": self._select(limit, max_bytes, unassigned=False)}

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

    def claim_delivery(self, limit=50, lease_seconds=60, max_bytes=8 * 1024 * 1024):
        """Lease one persisted, single-table HTTP delivery without regrouping it."""
        integer(limit, "limit")
        integer(lease_seconds, "lease_seconds", 1, 3600)
        integer(max_bytes, "max_bytes", 1024, 16 * 1024 * 1024)
        with self.connection:
            self._expire()  # Acquires the write lock before selecting/preparing.
            delivery = self.connection.execute(
                "SELECT * FROM deliveries AS d WHERE state='pending' AND NOT EXISTS ("
                "SELECT 1 FROM rows, json_each(d.row_ids) AS member "
                "WHERE rows.id=member.value AND rows.status='inflight') "
                "ORDER BY created_at,delivery_id LIMIT 1"
            ).fetchone()
            if delivery is not None:
                ids = json.loads(delivery["row_ids"])
                records = self.connection.execute(
                    "SELECT rows.* FROM rows JOIN json_each(?) AS member "
                    "ON rows.id=member.value ORDER BY CAST(member.key AS INTEGER)",
                    (delivery["row_ids"],),
                ).fetchall()
                if len(records) != len(ids) or any(
                    r["status"] != "pending" for r in records
                ):
                    raise BufferError("conflict", "saved delivery rows are unavailable")
                selected = [
                    {
                        "id": r["id"],
                        "table": r["table_name"],
                        "row": json.loads(r["payload"]),
                        "created_at": r["created_at"],
                    }
                    for r in records
                ]
                size = sum(
                    len(json.dumps(r, separators=(",", ":"), allow_nan=False).encode())
                    for r in selected
                )
                if len(selected) > limit or size > max_bytes:
                    raise BufferError(
                        "too_large",
                        "saved delivery exceeds requested limits; cannot split it",
                    )
                delivery_id, table = delivery["delivery_id"], delivery["table_name"]
            else:
                first = self._select(1, max_bytes)
                if not first:
                    return {
                        "message_type": "buffer.delivery.v1",
                        "delivery_id": None,
                        "table": None,
                        "lease_id": None,
                        "expires_at": None,
                        "rows": [],
                    }
                table = first[0]["table"]
                selected = self._select(limit, max_bytes, table_name=table)
                ids = [r["id"] for r in selected]
                delivery_id = str(uuid.uuid4())
                self.connection.execute(
                    "INSERT INTO deliveries VALUES (?,?,?,'pending',?,NULL)",
                    (delivery_id, table, json.dumps(ids), time.time()),
                )
            token, now = uuid.uuid4().hex, time.time()
            self.connection.execute(
                "INSERT INTO leases VALUES (?,'active',?,?,?,?)",
                (token, now + lease_seconds, json.dumps(ids), "[]", now),
            )
            self.connection.executemany(
                "UPDATE rows SET status='inflight',lease_id=?,lease_until=? WHERE id=?",
                [(token, now + lease_seconds, identity) for identity in ids],
            )
        return {
            "message_type": "buffer.delivery.v1",
            "delivery_id": delivery_id,
            "table": table,
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
            completed = []
            for delivery in self.connection.execute(
                "SELECT delivery_id,row_ids FROM deliveries WHERE state='pending'"
            ):
                delivery_ids = set(json.loads(delivery["row_ids"]))
                if not delivery_ids & fresh:
                    continue
                if not delivery_ids <= fresh:
                    raise BufferError(
                        "invalid", "delivery must be acknowledged in full"
                    )
                completed.append(delivery["delivery_id"])
            self.connection.executemany(
                "DELETE FROM rows WHERE id=? AND status='inflight' AND lease_id=?",
                [(identity, lease_id) for identity in fresh],
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
            self.connection.executemany(
                "UPDATE deliveries SET state='completed',completed_at=? WHERE delivery_id=?",
                [(time.time(), identity) for identity in completed],
            )
            self._count("sent_rows_total", len(fresh))
            self._count("purged_sent_total", len(fresh))
            # ACK response replay is a bounded cache, never seven days of history.
            self.connection.execute(
                "DELETE FROM leases WHERE token IN (SELECT token FROM leases "
                "WHERE state='acked' ORDER BY created_at DESC LIMIT -1 OFFSET 1024)"
            )
            self.connection.executemany(
                "DELETE FROM deliveries WHERE delivery_id=?", [(i,) for i in completed]
            )
        # The service schedules cleanup after replying, not in this ACK path.
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
                "DELETE FROM leases WHERE token=? AND state='active'",
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

    def maintain(self):
        """Bounded maintenance; never discard pending/inflight rows or source fences."""
        now = time.time()
        with self.connection:
            self._expire()
            # Completed ACK replay tokens live only until the original lease expires.
            # Limit each pass so cleanup cannot become one unbounded write.
            self.connection.execute(
                "DELETE FROM leases WHERE token IN (SELECT token FROM leases "
                "WHERE state!='active' AND expires_at<=? ORDER BY created_at LIMIT 256)",
                (now,),
            )
            self.connection.execute(
                "DELETE FROM deliveries WHERE delivery_id IN (SELECT delivery_id FROM deliveries "
                "WHERE state='completed' ORDER BY completed_at LIMIT 256)"
            )
        # WAL readers keep serving during cleanup; checkpoint never waits for readers.
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        # Free pages remain reusable even if physical high-water size has not shrunk.
        self.connection.execute("PRAGMA incremental_vacuum(64)").fetchall()

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
            "main_bytes": self.path.stat().st_size,
            "wal_bytes": (
                Path(str(self.path) + "-wal").stat().st_size
                if Path(str(self.path) + "-wal").exists()
                else 0
            ),
            "max_size_mb": self.max_bytes / 1024 / 1024,
            "live_bytes": (
                self.connection.execute("PRAGMA page_count").fetchone()[0]
                - self.connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            * self.connection.execute("PRAGMA page_size").fetchone()[0],
            "producers": self.connection.execute(
                "SELECT COUNT(*) FROM producers"
            ).fetchone()[0],
            "over_limit": disk > self.max_bytes,
            "pending_deliveries": self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE state='pending'"
            ).fetchone()[0],
            "source_batches": self.connection.execute(
                "SELECT COUNT(*) FROM source_batches"
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
