"""Persistent buffer deliveries, expiring leases and retry backoff."""

import asyncio
import logging
import threading
import time
import uuid

from ..messaging.backend import Backend
from ..messaging.buffer import Client, BufferError

log = logging.getLogger(__name__)


class IngestWorker(threading.Thread):
    def __init__(
        self,
        api,
        endpoint,
        stop,
        *,
        poll_interval=1,
        timeout_ms=2000,
        max_flushes=None,
        claim_max_bytes=8 * 1024 * 1024,
    ):
        self.claim_max_bytes = claim_max_bytes
        super().__init__(name="ingest-worker", daemon=True)
        self.api, self.endpoint, self.stop = api, endpoint, stop
        self.poll_interval, self.timeout_ms, self.max_flushes = (
            poll_interval,
            timeout_ms,
            max_flushes,
        )
        self.lease_seconds = max(
            60, api.request.timeout_seconds + 15 + 6 * timeout_ms // 1000
        )
        if self.lease_seconds > 3600:
            raise ValueError("request timeout exceeds supported buffer lease duration")
        if not 1 <= api.ingest.batch_size <= 1000:
            raise ValueError("ingest.batch_size must be 1..1000 for buffer claims")
        self.error = None
        self.lock = threading.Lock()
        self.current_lease = None
        self.pending_ack = None
        self.status = {
            "state": "loading",
            "since": time.monotonic(),
            "heartbeat_at_ns": time.time_ns(),
            "processed": 0,
            "processed_at_ns": time.time_ns(),
            "flushes": 0,
            "rows_sent_total": 0,
            "http_rows_accepted_total": 0,
            "http_requests_total": 0,
            "failures_total": 0,
            "consecutive_failures": 0,
            "ambiguous_rows_total": 0,
            "ack_pending_rows": 0,
            "last_http_ms": 0,
            "last_http_status": None,
            "pending": None,
            "dependency_error": None,
            "blocked_batch_id": None,
            "buffer_available": None,
            "backend_available": None,
        }

    def update(self, **values):
        with self.lock:
            self.status.update(values)
            self.status["heartbeat_at_ns"] = time.time_ns()

    def state(self, value):
        self.update(state=value, since=time.monotonic())

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    async def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < deadline:
            self.update()
            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))

    def backoff(self):
        failures = self.snapshot()["consecutive_failures"]
        if not failures:
            return float(self.api.ingest.flush_interval_seconds)
        return self.api.buffer.retry_interval_seconds * min(
            2 ** min(failures - 1, 3), 5
        )

    def failed(self, reason, **fields):
        status = self.snapshot()
        count = status["consecutive_failures"] + 1
        self.update(
            consecutive_failures=count,
            failures_total=status["failures_total"] + 1,
            dependency_error=reason,
        )
        log.warning(
            "ingest delivery deferred",
            extra={
                "stage": "output",
                "event": "delivery_deferred",
                "fields": {
                    "reason": reason,
                    "failures": count,
                    "retry_seconds": self.backoff(),
                    **fields,
                },
            },
        )
        if count >= self.api.request.max_consecutive_failures:
            log.warning("ingest failure threshold reached; rows retained for retry")

    def retry_ack(self, client):
        if self.pending_ack is None:
            return True
        token, ids = self.pending_ack
        try:
            result = client.request("ack", lease_id=token, ids=ids)
            if result.get("acked") != len(ids):
                raise ValueError("invalid acknowledgement count")
        except (TimeoutError, BufferError) as exc:
            if isinstance(exc, BufferError) and exc.code in (
                "lease_expired",
                "invalid_lease",
            ):
                self.update(
                    ambiguous_rows_total=self.snapshot()["ambiguous_rows_total"]
                    + len(ids),
                    ack_pending_rows=0,
                )
                self.pending_ack = None
                self.current_lease = None
                self.failed("backend_success_buffer_ack_expired", rows=len(ids))
            else:
                self.failed("buffer_ack_unconfirmed", rows=len(ids))
            return False
        self.update(
            rows_sent_total=self.snapshot()["rows_sent_total"] + len(ids),
            ack_pending_rows=0,
            buffer_available=True,
        )
        self.pending_ack = None
        return True

    def release(self, client):
        if self.current_lease is not None and self.pending_ack is None:
            try:
                client.request("release", lease_id=self.current_lease)
            except (TimeoutError, BufferError):
                pass  # Expiry recovers rows if release cannot reach buffer.
            self.current_lease = None

    async def flush(self, client, backend):
        if self.snapshot()["blocked_batch_id"] is not None:
            self.state("blocked")
            return False
        self.state("flushing")
        delivery = client.request(
            "claim_delivery",
            limit=self.api.ingest.batch_size,
            lease_seconds=self.lease_seconds,
            max_bytes=self.claim_max_bytes,
        )
        if (
            not isinstance(delivery, dict)
            or delivery.get("message_type") != "buffer.delivery.v1"
            or not isinstance(delivery.get("rows"), list)
        ):
            raise ValueError("invalid buffer delivery")
        if not delivery["rows"]:
            return False
        if not isinstance(delivery.get("lease_id"), str) or not delivery["lease_id"]:
            raise ValueError("missing lease")
        self.current_lease = delivery["lease_id"]
        try:
            batch_id, table, records = (
                delivery.get("delivery_id"),
                delivery.get("table"),
                delivery["rows"],
            )
            if not isinstance(batch_id, str):
                raise ValueError("missing delivery UUID")
            identity = uuid.UUID(batch_id)
            if identity.version != 4 or str(identity) != batch_id:
                raise ValueError("invalid delivery UUIDv4")
            if not isinstance(table, str) or not table:
                raise ValueError("invalid delivery table")
            identities = set()
            for record in records:
                if (
                    not isinstance(record, dict)
                    or type(record.get("id")) is not int
                    or record["id"] < 1
                    or record["id"] in identities
                    or record.get("table") != table
                    or not isinstance(record.get("row"), dict)
                ):
                    raise ValueError("invalid delivery row")
                identities.add(record["id"])
            self.update(flushes=self.snapshot()["flushes"] + 1)
            if self.stop.is_set():
                return False
            # The buffer owns the UUID and grouping; never repackage retries here.
            client.request(
                "renew",
                lease_id=self.current_lease,
                lease_seconds=self.lease_seconds,
            )
            self.state("posting")
            result = await backend.push(table, records, batch_id=batch_id)
            status = self.snapshot()
            self.update(
                http_requests_total=status["http_requests_total"] + 1,
                last_http_ms=result.elapsed_ms,
                last_http_status=result.status_code,
                backend_available=result.success,
            )
            if not result.success:
                if result.reason == "batch_id_conflict":
                    self.update(
                        blocked_batch_id=batch_id,
                        dependency_error="batch_id_conflict",
                        pending=None,
                        consecutive_failures=status["consecutive_failures"] + 1,
                        failures_total=status["failures_total"] + 1,
                    )
                    self.state("blocked")
                    log.error(
                        "batch ID conflict; delivery paused and rows retained for investigation",
                        extra={
                            "stage": "output",
                            "event": "batch_id_conflict",
                            "fields": {
                                "batch_id": batch_id,
                                "table": table,
                                "rows": len(records),
                                "http_status": result.status_code,
                            },
                        },
                    )
                    return True
                if result.ambiguous:
                    self.update(
                        ambiguous_rows_total=self.snapshot()["ambiguous_rows_total"]
                        + len(records)
                    )
                log.warning(
                    "backend request not confirmed",
                    extra={
                        "stage": "output",
                        "event": "http_failed",
                        "fields": {
                            "batch_id": batch_id,
                            "table": table,
                            "rows": len(records),
                            "http_status": result.status_code,
                            "reason": result.reason,
                        },
                    },
                )
                self.failed("backend_request_failed", batch_id=batch_id)
                return True
            self.update(
                http_rows_accepted_total=self.snapshot()["http_rows_accepted_total"]
                + len(records)
            )
            self.pending_ack = (
                self.current_lease,
                [record["id"] for record in records],
            )
            self.update(ack_pending_rows=len(records))
            if not self.retry_ack(client):
                self.state("ack_pending")
                return True
            log.info(
                "table rows delivered",
                extra={
                    "stage": "output",
                    "event": "rows_delivered",
                    "fields": {
                        "batch_id": batch_id,
                        "table": table,
                        "rows": len(records),
                        "http_ms": round(result.elapsed_ms, 2),
                    },
                },
            )
            self.update(consecutive_failures=0, dependency_error=None)
        finally:
            self.release(client)
            self.update(
                processed=self.snapshot()["processed"] + 1,
                processed_at_ns=time.time_ns(),
            )
        return True

    async def loop(self):
        client = Client(self.endpoint, self.timeout_ms)
        backend = Backend(self.api)
        next_flush = time.monotonic()
        try:
            while not self.stop.is_set():
                if self.snapshot()["blocked_batch_id"] is not None:
                    self.state("blocked")
                    await self.wait(self.poll_interval)
                    continue
                if self.pending_ack is not None:
                    self.state("ack_pending")
                    if self.retry_ack(client):
                        self.release(client)
                        self.update(consecutive_failures=0, dependency_error=None)
                    else:
                        await self.wait(self.poll_interval)
                        continue
                if (
                    self.snapshot()["consecutive_failures"]
                    and time.monotonic() < next_flush
                ):
                    self.state("backoff")
                    await self.wait(
                        min(self.poll_interval, next_flush - time.monotonic())
                    )
                    continue
                self.state("waiting")
                try:
                    stats = client.request("stats")
                    pending = stats.get("pending")
                    if type(pending) is not int or pending < 0:
                        raise ValueError("invalid buffer pending count")
                    self.update(
                        pending=pending,
                        buffer_available=True,
                        processed_at_ns=time.time_ns(),
                    )
                    if self.snapshot()["dependency_error"] == "buffer_unavailable":
                        self.update(consecutive_failures=0, dependency_error=None)
                    if pending and (
                        pending >= self.api.ingest.batch_size
                        or time.monotonic() >= next_flush
                    ):
                        did_flush = await self.flush(client, backend)
                        next_flush = time.monotonic() + self.backoff()
                        if (
                            self.max_flushes
                            and self.snapshot()["flushes"] >= self.max_flushes
                        ):
                            break
                        if (
                            did_flush
                            and not self.snapshot()["consecutive_failures"]
                            and self.pending_ack is None
                            and pending >= self.api.ingest.batch_size
                        ):
                            continue  # Drain a full backlog without adding a poll-interval delay per batch.
                    await self.wait(self.poll_interval)
                except (TimeoutError, BufferError):
                    self.update(buffer_available=False)
                    self.failed("buffer_unavailable")
                    next_flush = time.monotonic() + self.backoff()
                    await self.wait(self.poll_interval)
        finally:
            self.release(client)
            await backend.close()
            client.close()

    def run(self):
        try:
            asyncio.run(self.loop())
        except Exception as exc:
            self.error = exc
            log.exception("ingest worker failed; unacknowledged rows remain buffered")
        finally:
            self.state("failed" if self.error else "stopped")
            self.stop.set()
