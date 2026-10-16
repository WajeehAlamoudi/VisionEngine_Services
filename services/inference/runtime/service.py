"""Coordinate model inference, input reception, health, and safe process recovery."""

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
from ..processing.model import YoloModel
from ..processing.worker import FrameInbox, InferenceWorker

log = logging.getLogger("inference")


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
    result.add_argument("--model-id", required=True)
    result.add_argument(
        "--input",
        action="append",
        required=True,
        help="CAMERA_ID=tcp://HOST:PORT; repeat for shared inference",
    )
    result.add_argument("--bind", default="tcp://0.0.0.0:5556")
    result.add_argument(
        "--annotated-bind", help="optional frame output, for example tcp://0.0.0.0:5557"
    )
    result.add_argument(
        "--max-items", type=positive_int, default=2, help="queue limit per camera"
    )
    result.add_argument(
        "--max-bytes",
        type=positive_int,
        default=16_777_216,
        help="pixel byte limit per camera",
    )
    result.add_argument(
        "--max-age-ms", type=nonnegative, default=500, help="0 disables queue expiry"
    )
    result.add_argument("--native-timeout", type=positive, default=120)
    result.add_argument("--shutdown-timeout", type=positive, default=10)
    result.add_argument("--max-frames", type=positive_int)
    result.add_argument(
        "--health-file", type=Path, default=os.environ.get("INFERENCE_HEALTH_FILE")
    )
    add_arguments(result)
    return result


def log_status(status, previous, elapsed):
    log.info(
        "service status",
        extra={
            "event": "service_status",
            "fields": {
                "model_id": status["model_id"],
                "state": status["state"],
                "processed_total": status["processed"],
            },
        },
    )
    for camera_id, queue in status["inputs"].items():
        before = previous.get(camera_id, {})
        processed = status["per_camera"][camera_id]
        context = {"camera_id": camera_id, "model_id": status["model_id"]}
        log.info(
            "receiver status",
            extra={
                "stage": "input",
                "event": "receiver_status",
                "fields": {
                    **context,
                    "received_total": queue["received"],
                    "fps": round(
                        (queue["received"] - before.get("received", 0)) / elapsed, 2
                    ),
                    "queued_frames": queue["queued"],
                    "queued_bytes": queue["queued_bytes"],
                    "dropped_total": queue["dropped_local"],
                },
            },
        )
        log.info(
            "model status",
            extra={
                "stage": "processing",
                "event": "model_status",
                "fields": {
                    **context,
                    "processed_total": processed,
                    "fps": round((processed - before.get("processed", 0)) / elapsed, 2),
                    "last_inference_ms": status["last_inference_ms"][camera_id],
                },
            },
        )
        log.info(
            "publisher status",
            extra={
                "stage": "output",
                "event": "publisher_status",
                "fields": {
                    **context,
                    "detections_sent_total": processed,
                    "annotated_sent_total": status["annotated_sent"][camera_id],
                },
            },
        )
        previous[camera_id] = {"received": queue["received"], "processed": processed}


def write_status(path, status):
    if path is None:
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({**status, "updated_at": time.time()}), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        log.warning("could not update inference health file")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def run(args, *, model=None):
    configuration, inputs = load_settings(args.config, args.model_id, args.input)
    model = model or YoloModel(configuration)
    stop = threading.Event()
    inbox = FrameInbox(inputs, args.max_items, args.max_bytes, args.max_age_ms)
    subscriber = Subscriber(inputs, inbox, stop)
    worker = InferenceWorker(
        model,
        inputs,
        inbox,
        stop,
        bind=args.bind,
        annotated_bind=args.annotated_bind,
        max_frames=args.max_frames,
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
                status["state"] in ("loading", "inferring", "closing")
                and time.monotonic() - status["since"] > args.native_timeout
            ):
                log.error(
                    "inference native operation timed out; process restart required"
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
        "inference",
        "inference-" + args.model_id.replace("_", "-").lower(),
        model_id=args.model_id,
    )
    try:
        code, stuck = run(args)
    except Exception as exc:
        log.error(
            "inference startup failed (%s): %s", type(exc).__name__, str(exc)[:300]
        )
        return 1
    if stuck:
        log.error("inference thread did not stop; exiting for supervisor recovery")
        logging.shutdown()
        os._exit(1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
