"""Coordinate per-camera enrichment, input reception, health, and safe process recovery."""

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
from .logging import add_arguments, configure
from ..messaging.subscriber import Subscriber
from ..processing.worker import Inbox, EnrichmentWorker

log = logging.getLogger("enrichment")


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
    result.add_argument("--camera-id", required=True)
    result.add_argument(
        "--input", required=True, help="detections endpoint: tcp://HOST:PORT"
    )
    result.add_argument("--bind", default="tcp://0.0.0.0:5559")
    result.add_argument("--max-items", type=positive_int, default=32)
    result.add_argument("--max-bytes", type=positive_int, default=4_194_304)
    result.add_argument("--max-age-ms", type=nonnegative, default=5000)
    result.add_argument("--native-timeout", type=positive, default=120)
    result.add_argument("--shutdown-timeout", type=positive, default=10)
    result.add_argument("--max-frames", type=positive_int)
    result.add_argument(
        "--health-file", type=Path, default=os.environ.get("ENRICHMENT_HEALTH_FILE")
    )
    add_arguments(result)
    return result


def log_status(status, previous, elapsed):
    log.info(
        "receiver status",
        extra={
            "stage": "input",
            "event": "receiver_status",
            "fields": status["inputs"],
        },
    )
    log.info(
        "enrichment status",
        extra={
            "stage": "processing",
            "event": "enrichment_status",
            "fields": {
                "state": status["state"],
                "processed_total": status["processed"],
                "fps": round(
                    (status["processed"] - previous.get("processed", 0)) / elapsed, 2
                ),
                "enrichment_ms": round(status.get("last_enrichment_ms", 0), 2),
                "events_total": status["events_total"],
                "stale_total": status["stale_total"],
            },
        },
    )
    log.info(
        "publisher status",
        extra={
            "stage": "output",
            "event": "publisher_status",
            "fields": {"sent_total": status["sent_total"]},
        },
    )
    previous["processed"] = status["processed"]


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
        log.warning("could not update enrichment health file")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def run(args):
    camera, model = load_settings(args.config, args.camera_id)
    for endpoint in (args.input, args.bind):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("endpoints must use TCP or IPC")
    stop = threading.Event()
    inbox = Inbox(args.max_items, args.max_bytes, args.max_age_ms)
    subscriber = Subscriber(camera, model, args.input, inbox, stop)
    worker = EnrichmentWorker(
        camera, inbox, stop, args.bind, max_frames=args.max_frames
    )
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    worker.start()
    subscriber.start()
    code, next_status = 0, 0
    previous, previous_status_time = {}, time.monotonic()
    try:
        while not stop.wait(0.1):
            status = worker.snapshot()
            if (
                status["state"] in ("loading", "enriching", "closing")
                and time.monotonic() - status["since"] > args.native_timeout
            ):
                log.error(
                    "enrichment native operation timed out; process restart required"
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
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        write_status(
            args.health_file,
            {
                **worker.snapshot(),
                "state": "failed"
                if code or worker.error or subscriber.error
                else "stopped",
            },
        )
    return code or int(
        bool(worker.error or subscriber.error)
    ), worker.is_alive() or subscriber.is_alive()


def main():
    args = parser().parse_args()
    configure(
        args,
        "enrichment",
        "enrichment-" + args.camera_id.replace("_", "-").lower(),
        camera_id=args.camera_id,
    )
    try:
        code, stuck = run(args)
    except Exception as exc:
        log.error(
            "enrichment startup failed (%s): %s", type(exc).__name__, str(exc)[:300]
        )
        return 1
    if stuck:
        log.error("enrichment thread did not stop; exiting for supervisor recovery")
        logging.shutdown()
        os._exit(1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
