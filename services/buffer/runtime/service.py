"""Single-owner SQLite buffer service: commit, acknowledge, lease and inspect."""

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

import zmq

from .configuration import load_settings
from .logging import add_arguments, configure
from ..storage.database import Database, integer
from ..messaging.protocol import BufferError, decode, encode, MAX_MESSAGE_BYTES
from ..messaging.publisher import Publisher

log = logging.getLogger("buffer")


def dispatch(database, request):
    if (
        request.get("version") != 1
        or not isinstance(request.get("request_id"), str)
        or not 1 <= len(request["request_id"]) <= 64
    ):
        raise BufferError("invalid", "invalid request envelope")
    op = request.get("op")
    common = {"version", "request_id", "op"}
    fields = {
        "put": {"batch_id", "batch"},
        "claim": {"limit", "lease_seconds", "max_bytes"},
        "peek": {"limit", "max_bytes"},
        "ack": {"lease_id", "ids"},
        "release": {"lease_id"},
        "renew": {"lease_id", "lease_seconds"},
        "stats": set(),
    }
    if op not in fields or set(request) - common - fields[op]:
        raise BufferError("invalid", "unknown operation or fields")
    arguments = {key: value for key, value in request.items() if key not in common}
    if op in ("ack", "release", "renew") and (
        not isinstance(arguments.get("lease_id"), str)
        or len(arguments["lease_id"]) > 64
    ):
        raise BufferError("invalid", "invalid lease ID")
    return getattr(database, op)(**arguments)


def write_status(path, document):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=Path("config/YAMLs"))
    result.add_argument("--instance-id", default="buffer")
    result.add_argument("--bind", default="tcp://0.0.0.0:5564")
    result.add_argument("--status-bind", default="tcp://0.0.0.0:5565")
    result.add_argument(
        "--database", type=Path, help="Override api.buffer.path for isolated testing"
    )
    result.add_argument("--receipt-days", type=int, default=7)
    result.add_argument("--max-requests", type=int)
    result.add_argument(
        "--health-file",
        type=Path,
        default=Path(os.environ.get("BUFFER_HEALTH_FILE", "/tmp/buffer-health.json")),
    )
    add_arguments(result)
    return result


def run(args):
    api, device = load_settings(args.config)
    for endpoint in (args.bind, args.status_bind):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("buffer endpoints must use TCP or IPC")
    integer(args.receipt_days, "receipt_days", 1, 365)
    if args.max_requests is not None:
        integer(args.max_requests, "max_requests", 1, 1_000_000)
    database = Database(
        args.database or api.buffer.path,
        max_size_mb=api.buffer.max_size_mb,
        delete_after_hours=api.buffer.delete_after_hours,
        receipt_days=args.receipt_days,
    )
    publisher = None
    stop, handlers = threading.Event(), {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    requests = rejected = cycles = code = 0
    next_maintenance = next_status = time.monotonic()
    previous_drops = 0
    try:
        publisher = Publisher(args.status_bind)
        with zmq.Context() as context:
            with context.socket(zmq.REP) as socket:
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
                socket.bind(args.bind)
                while not stop.is_set():
                    if socket.poll(100):
                        request = {}
                        parts = socket.recv_multipart()
                        try:
                            if len(parts) != 1:
                                raise BufferError(
                                    "invalid", "expected one request frame"
                                )
                            request = decode(parts[0])
                            result = dispatch(database, request)
                            reply = {
                                "version": 1,
                                "request_id": request.get("request_id"),
                                "ok": True,
                                "result": result,
                            }
                        except (
                            ValueError,
                            TypeError,
                            KeyError,
                            OverflowError,
                            sqlite3.Error,
                        ) as exc:
                            rejected += 1
                            error = (
                                exc.code
                                if isinstance(exc, BufferError)
                                else "storage"
                                if isinstance(exc, sqlite3.Error)
                                else "invalid"
                            )
                            reply = {
                                "version": 1,
                                "request_id": request.get("request_id"),
                                "ok": False,
                                "code": error,
                                "error": str(exc)[:200]
                                if isinstance(exc, BufferError)
                                else "buffer request validation or storage failed",
                            }
                            log.warning(
                                "request rejected",
                                extra={
                                    "stage": "input",
                                    "event": "request_rejected",
                                    "fields": {"code": error},
                                },
                            )
                        socket.send(encode(reply))
                        requests += 1
                    now = time.monotonic()
                    if now >= next_maintenance:
                        database.maintain()
                        cycles += 1
                        next_maintenance = now + 5
                    if now >= next_status:
                        stats = database.stats()
                        status = {
                            "state": "waiting",
                            "updated_at": time.time(),
                            "processed_at_ns": time.time_ns(),
                            "processed": cycles,
                            "requests_total": requests,
                            "rejected_total": rejected,
                            **stats,
                        }
                        write_status(args.health_file, status)
                        publisher.publish(device.id, stats)
                        log.info(
                            "buffer status",
                            extra={
                                "stage": "processing",
                                "event": "buffer_status",
                                "fields": stats,
                            },
                        )
                        log.info(
                            "request status",
                            extra={
                                "stage": "input",
                                "event": "request_status",
                                "fields": {"received": requests, "rejected": rejected},
                            },
                        )
                        log.info(
                            "delivery status",
                            extra={
                                "stage": "output",
                                "event": "delivery_status",
                                "fields": {
                                    "sent_rows": stats["sent_rows_total"],
                                    "pending": stats["pending"],
                                    "inflight": stats["inflight"],
                                },
                            },
                        )
                        if (
                            stats["dropped_pending_total"] > previous_drops
                            or stats["over_limit"]
                        ):
                            log.warning(
                                "buffer capacity pressure",
                                extra={
                                    "event": "capacity_pressure",
                                    "fields": {
                                        "dropped_pending_total": stats[
                                            "dropped_pending_total"
                                        ],
                                        "over_limit": stats["over_limit"],
                                    },
                                },
                            )
                        previous_drops = stats["dropped_pending_total"]
                        next_status = now + 5
                    if args.max_requests and requests >= args.max_requests:
                        break
    except Exception:
        log.exception("buffer failed")
        code = 1
    finally:
        if publisher:
            publisher.close()
        write_status(
            args.health_file,
            {
                "state": "failed" if code else "stopped",
                "updated_at": time.time(),
                "processed_at_ns": time.time_ns(),
                "processed": cycles,
                **database.stats(),
            },
        )
        database.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    return code


def main():
    args = parser().parse_args()
    configure(args, "buffer", args.instance_id)
    try:
        return run(args)
    except Exception as exc:
        log.error("buffer startup failed (%s)", type(exc).__name__)
        return 1
