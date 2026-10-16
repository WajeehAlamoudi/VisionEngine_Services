"""Event-loop-owned bounded FIFO with oldest-first eviction and age expiry."""

from collections import deque
import time


class Inbox:
    def __init__(self, max_items=32, max_bytes=4194304, max_age_ms=5000):
        self.max_items, self.max_bytes, self.max_age = (
            max_items,
            max_bytes,
            max_age_ms / 1000,
        )
        self.items = deque()
        self.bytes = self.received = self.dropped = self.rejected = 0

    def drop(self):
        self.bytes -= self.items.popleft()[2]
        self.dropped += 1

    def expire(self):
        while (
            self.items
            and self.max_age
            and time.monotonic() - self.items[0][1] > self.max_age
        ):
            self.drop()

    def put(self, value, size):
        self.received += 1
        self.expire()
        if size > self.max_bytes:
            self.dropped += 1
            return
        while self.items and (
            len(self.items) >= self.max_items or self.bytes + size > self.max_bytes
        ):
            self.drop()
        self.items.append((value, time.monotonic(), size))
        self.bytes += size

    def pop(self):
        self.expire()
        if not self.items:
            return None
        value, _, size = self.items.popleft()
        self.bytes -= size
        return value

    def discard_all(self):
        while self.items:
            self.drop()

    def snapshot(self):
        self.expire()
        return {
            "received_total": self.received,
            "queued_batches": len(self.items),
            "queued_bytes": self.bytes,
            "dropped_total": self.dropped,
            "rejected_total": self.rejected,
        }
