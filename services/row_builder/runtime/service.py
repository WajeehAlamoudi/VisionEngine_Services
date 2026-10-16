"""Coordinate row building and routing, input reception, health, and safe process recovery."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time

from .configuration import load_settings, load_device
from .logging import add_arguments, configure
from ..messaging.subscriber import Subscriber
from ..processing.worker import Inbox, RowWorker
from ..processing.builder import RowBuilder
from .delivery import Journal, Delivery

log = logging.getLogger("row_builder")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("expected a finite nonnegative number")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=Path("config/YAMLs"))
    result.add_argument("--instance-id", default="row-builder")
    result.add_argument(
        "--events-input",
        action="append",
        default=[],
        help="Accepted-event publisher endpoint; repeat as needed",
    )
    result.add_argument(
        "--alerts-input",
        action="append",
        default=[],
        help="Alert publisher endpoint; repeat as needed",
    )
    result.add_argument("--bind", default="tcp://0.0.0.0:5562")
    result.add_argument("--buffer-connect", help="Acknowledged buffer RPC endpoint")
    result.add_argument(
        "--delivery-path", type=Path, default=Path("data/row-delivery.db")
    )
    result.add_argument("--delivery-max-items", type=positive_int, default=1000)
    result.add_argument("--delivery-max-bytes", type=positive_int, default=4_194_304)
    result.add_argument("--buffer-timeout-ms", type=positive_int, default=2000)
    result.add_argument("--buffer-retry-seconds", type=positive, default=1)
    result.add_argument(
        "--monitor-input",
        action="append",
        default=[],
        help="Monitor health.v1 publisher endpoint; repeat as needed",
    )
    result.add_argument("--max-items", type=positive_int, default=32)
    result.add_argument("--max-bytes", type=positive_int, default=4_194_304)
    result.add_argument("--max-age-ms", type=nonnegative, default=5000)
    result.add_argument("--native-timeout", type=positive, default=120)
    result.add_argument("--shutdown-timeout", type=positive, default=10)
    result.add_argument("--max-frames", type=positive_int)
    result.add_argument(
        "--health-file", type=Path, default=os.environ.get("ROW_BUILDER_HEALTH_FILE")
    )
    add_arguments(result)
    return result


def log_status(status, previous, elapsed):
    for key, queue in status["inputs"].items():
        log.info(
            "receiver status",
            extra={
                "stage": "input",
                "event": "receiver_status",
                "fields": {"input": key, **queue},
            },
        )
    log.info(
        "row builder status",
        extra={
            "stage": "processing",
            "event": "row_builder_status",
            "fields": {
                "state": status["state"],
                "processed": status["processed"],
                "fps": round(
                    (status["processed"] - previous.get("processed", 0)) / elapsed, 2
                ),
                "build_ms": round(status["last_build_ms"], 2),
                "detection_rows": status["detection_rows_total"],
                "notification_rows": status["notification_rows_total"],
                "node_rows": status["node_rows_total"],
                "skipped_disabled": status["skipped_disabled_total"],
                "skipped_no_table": status["skipped_no_table_total"],
                "stale_total": status["stale_total"],
            },
        },
    )
    log.info(
        "publisher status",
        extra={
            "stage": "output",
            "event": "publisher_status",
            "fields": {
                "batches_sent": status["sent_total"],
                "rows_sent": status["rows_total"],
            },
        },
    )
    previous["processed"] = status["processed"]
    if "delivery" in status:
        log.info(
            "buffer delivery status",
            extra={
                "stage": "output",
                "event": "buffer_delivery_status",
                "fields": status["delivery"],
            },
        )


def write_status(path, status):
    if path is None:
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({**status, "updated_at": time.time()}), encoding="utf-8"
        )
        # Windows scanners/readers can briefly hold the destination open.
        for attempt in range(4):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02)
    except OSError:
        log.warning("could not update row builder health file")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def run(args):
    cameras, models, rules, inputs = load_settings(
        args.config, args.events_input, args.alerts_input, args.monitor_input
    )
    if not args.bind.startswith(("tcp://", "ipc://")):
        raise ValueError("endpoints must use TCP or IPC")
    stop = threading.Event()
    device = load_device(args.config) if args.monitor_input else None
    builder = RowBuilder(cameras, models, rules, device=device)
    inbox = Inbox(inputs, args.max_items, args.max_bytes, args.max_age_ms)
    subscriber = Subscriber(inputs, builder, inbox, stop)
    journal = None
    delivery = None
    if args.buffer_connect:
        if not args.buffer_connect.startswith(("tcp://", "ipc://")):
            raise ValueError("buffer endpoint must use TCP or IPC")
        journal = Journal(
            args.delivery_path, args.delivery_max_items, args.delivery_max_bytes
        )
        delivery = Delivery(
            journal,
            args.buffer_connect,
            stop,
            timeout_ms=args.buffer_timeout_ms,
            retry_seconds=args.buffer_retry_seconds,
        )
    worker = RowWorker(
        builder,
        inputs,
        inbox,
        stop,
        args.bind,
        max_frames=args.max_frames,
        journal=journal,
    )
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    worker.start()
    subscriber.start()
    if delivery:
        delivery.start()
    code, next_status = 0, 0
    previous, previous_status_time = {}, time.monotonic()
    try:
        while not stop.wait(0.1):
            status = worker.snapshot()
            if delivery:
                status["delivery"] = delivery.snapshot()
            if (
                status["state"] in ("loading", "building", "closing")
                and time.monotonic() - status["since"] > args.native_timeout
            ):
                log.error(
                    "row builder native operation timed out; process restart required"
                )
                code = 1
                break
            if time.monotonic() >= next_status:
                status["inputs"] = inbox.snapshot()
                now = time.monotonic()
                log_status(status, previous, max(now - previous_status_time, 0.001))
                previous_status_time = now
                write_status(args.health_file, status)
                next_status = time.monotonic() + 5
    finally:
        stop.set()
        subscriber.join(args.shutdown_timeout)
        worker.join(args.shutdown_timeout)
        if delivery:
            delivery.join(args.shutdown_timeout)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        write_status(
            args.health_file,
            {
                **worker.snapshot(),
                "state": "failed"
                if code
                or worker.error
                or subscriber.error
                or (delivery and delivery.error)
                else "stopped",
            },
        )
        if journal and not worker.is_alive() and not delivery.is_alive():
            journal.close()
    return code or int(
        bool(worker.error or subscriber.error or (delivery and delivery.error))
    ), worker.is_alive() or subscriber.is_alive() or bool(
        delivery and delivery.is_alive()
    )


def main():
    args = parser().parse_args()
    configure(
        args,
        "row_builder",
        args.instance_id,
    )
    try:
        code, stuck = run(args)
    except Exception as exc:
        log.error(
            "row builder startup failed (%s): %s", type(exc).__name__, str(exc)[:300]
        )
        return 1
    if stuck:
        log.error("row builder thread did not stop; exiting for supervisor recovery")
        logging.shutdown()
        os._exit(1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
