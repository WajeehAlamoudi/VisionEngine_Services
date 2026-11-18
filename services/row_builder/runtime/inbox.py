"""Durable FIFO for validated wire payloads in the producer journal database."""

import json
import logging
import sqlite3
import threading

log = logging.getLogger(__name__)


class DurableInbox:
    def __init__(self, inputs, socket_limit, journal, capacity):
        self.max_items = socket_limit
        self.capacity = capacity
        self.space = journal.input_space
        self.available = threading.Event()
        self.lock = threading.Lock()
        self.blocked = None
        self.received = dict.fromkeys(inputs, 0)
        self.rejected = dict.fromkeys(inputs, 0)
        # Each connection has one active owner: subscriber, worker, or monitor.
        self.writer, self.reader, self.monitor = [
            sqlite3.connect(journal.path, check_same_thread=False, timeout=10)
            for _ in range(3)
        ]
        self.writer.execute("PRAGMA synchronous=FULL")
        # The delete trigger counts freed space in the same commit as promotion.
        self.writer.executescript("""
            CREATE TABLE IF NOT EXISTS incoming (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS input_cursor (
                source TEXT NOT NULL, identity TEXT NOT NULL, stream TEXT NOT NULL,
                sequence TEXT NOT NULL, received_ns TEXT NOT NULL, retired TEXT NOT NULL,
                PRIMARY KEY (source,identity)
            );
            CREATE TABLE IF NOT EXISTS incoming_usage (
                source TEXT PRIMARY KEY, batches INTEGER NOT NULL, bytes INTEGER NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS incoming_added AFTER INSERT ON incoming BEGIN
                INSERT INTO incoming_usage VALUES (NEW.source, 1, length(NEW.payload))
                ON CONFLICT(source) DO UPDATE SET batches=batches+1, bytes=bytes+excluded.bytes;
            END;
            CREATE TRIGGER IF NOT EXISTS incoming_removed AFTER DELETE ON incoming BEGIN
                UPDATE incoming_usage SET batches=batches-1, bytes=bytes-length(OLD.payload)
                WHERE source=OLD.source;
            END;
        """)

    def put_ready(self, messages, stop):
        # One subscriber admits inputs. Other connections can only free space.
        records = [(key, payload) for key, _, payload in messages]
        while records and not stop.is_set():
            self.space.clear()
            (used,) = self.writer.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM incoming_usage"
            ).fetchone()
            count = 0
            for key, payload in records:
                if len(payload) > self.capacity:
                    raise ValueError("input batch exceeds durable inbox capacity")
                if used + len(payload) > self.capacity:
                    break
                used += len(payload)
                count += 1
            if not count:
                with self.lock:
                    if self.blocked is None:
                        log.warning(
                            "durable input inbox at capacity; waiting for space"
                        )
                    self.blocked = records[0][0]
                self.space.wait(0.1)
                continue
            with self.writer:
                self.writer.executemany(
                    "INSERT INTO incoming(source,payload) VALUES (?,?)", records[:count]
                )
            with self.lock:
                self.blocked = None
                for key, _ in records[:count]:
                    self.received[key] += 1
            self.available.set()
            del records[:count]

    def pop(self, stop):
        while not stop.is_set():
            self.available.clear()
            row = self.reader.execute(
                "SELECT id,source,payload FROM incoming ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                record_id, source, payload = row
                return record_id, source, json.loads(payload)
            self.available.wait(0.1)
        return None

    def cursor(self, source, identity):
        row = self.reader.execute(
            "SELECT stream,sequence,received_ns,retired FROM input_cursor "
            "WHERE source=? AND identity=?",
            (source, identity),
        ).fetchone()
        if row:
            return row[0], int(row[1]), int(row[2]), json.loads(row[3])
        return None

    def reject(self, key):
        with self.lock:
            self.rejected[key] += 1

    def snapshot(self):
        usage = {
            key: (count, size)
            for key, count, size in self.monitor.execute(
                "SELECT source,batches,bytes FROM incoming_usage"
            )
        }
        with self.lock:
            return {
                key: {
                    "received_total": self.received.get(key, 0),
                    "queued_batches": usage.get(key, (0, 0))[0],
                    "queued_bytes": usage.get(key, (0, 0))[1],
                    "dropped_total": 0,
                    "rejected_total": self.rejected.get(key, 0),
                    "capacity_wait": self.blocked == key,
                }
                for key in self.received.keys() | usage.keys()
            }

    def close(self):
        for connection in (self.writer, self.reader, self.monitor):
            connection.close()
