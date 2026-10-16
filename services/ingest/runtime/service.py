"""Ingest lifecycle, independent health reporting and delivery status output."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from .configuration import load_settings
from .logging import configure, add_arguments
from ..processing.worker import IngestWorker
from ..messaging.publisher import Publisher

log = logging.getLogger("ingest")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive value")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def write_status(path, status):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps({**status, "updated_at": time.time()}, allow_nan=False),
            encoding="utf-8",
        )
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
    result.add_argument("--instance-id", default="ingest")
    result.add_argument("--buffer-connect", required=True)
    result.add_argument("--bind", default="tcp://0.0.0.0:5566")
    result.add_argument("--poll-interval", type=positive, default=1)
    result.add_argument("--buffer-timeout-ms", type=positive_int, default=2000)
    result.add_argument("--claim-max-bytes", type=positive_int, default=8 * 1024 * 1024)
    result.add_argument("--max-flushes", type=positive_int)
    result.add_argument("--shutdown-timeout", type=positive, default=15)
    result.add_argument(
        "--health-file",
        type=Path,
        default=Path(os.environ.get("INGEST_HEALTH_FILE", "/tmp/ingest-health.json")),
    )
    add_arguments(result)
    return result


def log_status(status):
    log.info(
        "buffer polling status",
        extra={
            "stage": "input",
            "event": "buffer_poll_status",
            "fields": {
                "pending": status["pending"],
                "buffer_available": status["buffer_available"],
            },
        },
    )
    log.info(
        "ingest status",
        extra={
            "stage": "processing",
            "event": "ingest_status",
            "fields": {
                "state": status["state"],
                "flushes": status["flushes"],
                "http_ms": round(status["last_http_ms"], 2),
                "failures": status["consecutive_failures"],
                "ack_pending_rows": status["ack_pending_rows"],
            },
        },
    )
    log.info(
        "backend delivery status",
        extra={
            "stage": "output",
            "event": "delivery_status",
            "fields": {
                "rows_sent": status["rows_sent_total"],
                "backend_available": status["backend_available"],
                "http_status": status["last_http_status"],
                "ambiguous_rows": status["ambiguous_rows_total"],
            },
        },
    )


def run(args):
    api, device = load_settings(args.config)
    if not 1024 <= args.claim_max_bytes <= 16 * 1024 * 1024:
        raise ValueError("claim-max-bytes must be between 1024 and 16 MiB")
    for endpoint in (args.buffer_connect, args.bind):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("endpoints must use TCP or IPC")
    stop = threading.Event()
    worker = IngestWorker(
        api,
        args.buffer_connect,
        stop,
        poll_interval=args.poll_interval,
        timeout_ms=args.buffer_timeout_ms,
        max_flushes=args.max_flushes,
        claim_max_bytes=args.claim_max_bytes,
    )
    publisher = Publisher(args.bind)
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    worker.start()
    next_status = 0
    code = 0
    max_work_age = max(30, api.request.timeout_seconds + 10)
    try:
        while not stop.wait(0.1):
            status = worker.snapshot()
            if (time.time_ns() - status["heartbeat_at_ns"]) / 1e9 > max_work_age + 30:
                log.error(
                    "ingest worker stopped progressing; supervisor recovery required"
                )
                code = 1
                break
            if time.monotonic() >= next_status:
                status["max_work_age_seconds"] = max_work_age
                write_status(args.health_file, status)
                publisher.publish(device.id, status)
                log_status(status)
                next_status = time.monotonic() + 5
    finally:
        stop.set()
        worker.join(args.shutdown_timeout)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        status = worker.snapshot()
        write_status(
            args.health_file,
            {
                **status,
                "state": "failed" if code or worker.error else "stopped",
                "max_work_age_seconds": max_work_age,
            },
        )
        publisher.close()
    return code or int(worker.error is not None), worker.is_alive()


def main():
    args = parser().parse_args()
    configure(args, "ingest", args.instance_id)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        code, stuck = run(args)
    except Exception as exc:
        log.error("ingest startup failed (%s)", type(exc).__name__)
        return 1
    if stuck:
        logging.shutdown()
        os._exit(1)
    return code
