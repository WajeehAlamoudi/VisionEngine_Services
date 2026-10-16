"""Collector lifecycle: isolated reception/storage, health, status and bounded native shutdown."""

import argparse
import logging
import os
from pathlib import Path
import signal
import threading
import time

from .configuration import load_settings
from .logging import configure, add_arguments
from ..processing.matcher import Matcher
from ..processing.worker import CollectorWorker
from ..messaging.subscriber import Subscriber
from ..messaging.publisher import Publisher

log = logging.getLogger("collector")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("config/YAMLs"))
    p.add_argument("--session-id", required=True)
    p.add_argument("--instance-id")
    p.add_argument("--frames-input", required=True)
    p.add_argument("--detections-input", required=True)
    p.add_argument("--bind", default="tcp://0.0.0.0:5568")
    p.add_argument("--output-dir", type=Path)
    for prefix, items, size in (("frame-", 64, 67108864), ("", 32, 4194304)):
        p.add_argument("--" + prefix + "max-items", type=positive, default=items)
        p.add_argument("--" + prefix + "max-bytes", type=positive, default=size)
        p.add_argument("--" + prefix + "max-age-ms", type=nonnegative, default=5000)
    p.add_argument("--min-free-mb", type=nonnegative, default=256)
    p.add_argument("--native-timeout", type=positive, default=60)
    p.add_argument("--shutdown-timeout", type=positive, default=10)
    p.add_argument("--max-batches", type=positive)
    p.add_argument(
        "--health-file",
        type=Path,
        default=Path(
            os.environ.get("COLLECTOR_HEALTH_FILE", "/tmp/collector-health.json")
        ),
    )
    add_arguments(p)
    return p


def write_status(path, status):
    import json

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


def log_status(status):
    log.info(
        "dataset input status",
        extra={
            "stage": "input",
            "event": "receiver_status",
            "fields": status["inputs"],
        },
    )
    log.info(
        "collection status",
        extra={
            "stage": "processing",
            "event": "collection_status",
            "fields": {
                k: status[k]
                for k in (
                    "state",
                    "processed",
                    "skipped_schedule",
                    "skipped_filters",
                    "skipped_sampling",
                    "failed_total",
                    "last_save_ms",
                )
            },
        },
    )
    log.info(
        "dataset output status",
        extra={
            "stage": "output",
            "event": "dataset_status",
            "fields": {
                k: status[k]
                for k in (
                    "saved_total",
                    "saved_this_run",
                    "labels_total",
                    "duplicate_saved_total",
                    "recovered_total",
                    "quarantined_total",
                )
            },
        },
    )


def run(args):
    for endpoint in (args.frames_input, args.detections_input, args.bind):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("expected TCP or IPC endpoint")
    if (
        max(args.max_items, args.frame_max_items) > 1000
        or args.frame_max_bytes > 512 * 1024 * 1024
        or args.max_bytes > 16 * 1024 * 1024
    ):
        raise ValueError("collector cache limits exceed supported bounds")
    collection, session, camera, model = load_settings(args.config, args.session_id)
    stop = threading.Event()
    inbox = Matcher(
        camera.id,
        model.id,
        frame_max_items=args.frame_max_items,
        frame_max_bytes=args.frame_max_bytes,
        frame_max_age_ms=args.frame_max_age_ms,
        max_items=args.max_items,
        max_bytes=args.max_bytes,
        max_age_ms=args.max_age_ms,
    )
    worker = CollectorWorker(
        collection,
        session,
        camera,
        model,
        inbox,
        stop,
        output_dir=args.output_dir,
        min_free_bytes=args.min_free_mb * 1024 * 1024,
        max_batches=args.max_batches,
    )
    subscriber = Subscriber(
        camera.id, args.frames_input, args.detections_input, inbox, worker, stop
    )
    publisher = Publisher(args.bind)
    handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    worker.start()
    subscriber.start()
    next_status = 0
    code = 0
    try:
        while not stop.wait(0.1):
            status = worker.snapshot()
            if (time.time_ns() - status["heartbeat_at_ns"]) / 1e9 > args.native_timeout:
                log.error(
                    "collector native operation stopped progressing; supervisor recovery required"
                )
                code = 1
                break
            if time.monotonic() >= next_status:
                status["inputs"] = inbox.snapshot()
                status["max_work_age_seconds"] = args.native_timeout
                write_status(args.health_file, status)
                publisher.publish(session.id, status)
                log_status(status)
                next_status = time.monotonic() + 5
    finally:
        stop.set()
        subscriber.join(args.shutdown_timeout)
        worker.join(args.shutdown_timeout)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        status = {
            **worker.snapshot(),
            "inputs": inbox.snapshot(),
            "state": "failed"
            if code or worker.error or subscriber.error
            else "stopped",
        }
        write_status(args.health_file, status)
        publisher.close()
    return code or int(
        bool(worker.error or subscriber.error)
    ), worker.is_alive() or subscriber.is_alive()


def main():
    args = parser().parse_args()
    configure(
        args,
        "collector",
        args.instance_id or "collector-" + args.session_id.replace("_", "-"),
        session_id=args.session_id,
    )
    try:
        code, stuck = run(args)
    except Exception as exc:
        log.error(
            "collector startup failed (%s): %s", type(exc).__name__, str(exc)[:250]
        )
        return 1
    if stuck:
        logging.shutdown()
        os._exit(1)
    return code
