"""Collect service snapshots and publish device metadata independently of row mapping."""

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time
import uuid

from .configuration import load_settings
from .logging import add_arguments, configure
from ..processing.collector import Collector
from ..messaging.publisher import Publisher

log = logging.getLogger("monitor")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def atomic_json(path, document):
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
    result.add_argument("--instance-id", default="monitor")
    result.add_argument(
        "--watch",
        action="append",
        default=[],
        help="SERVICE=PATH health snapshot; repeat",
    )
    result.add_argument(
        "--camera-stage",
        action="append",
        default=[],
        help="CAMERA=SERVICE; repeat per required stage",
    )
    result.add_argument(
        "--camera-counter",
        action="append",
        default=[],
        help="CAMERA=RULES_SERVICE accepted counter",
    )
    result.add_argument("--bind", default="tcp://0.0.0.0:5563")
    result.add_argument("--sample-interval", type=positive, default=5)
    result.add_argument(
        "--publish-interval",
        type=positive,
        help="Override device heartbeat interval for testing",
    )
    result.add_argument("--max-age", type=positive, default=30)
    result.add_argument(
        "--buffer-source",
        default="buffer",
        help="Watched service supplying actual buffer pending count",
    )
    result.add_argument("--max-samples", type=int)
    result.add_argument(
        "--health-file",
        type=Path,
        default=Path(os.environ.get("MONITOR_HEALTH_FILE", "/tmp/monitor-health.json")),
    )
    add_arguments(result)
    return result


def run(args):
    if not args.bind.startswith(("tcp://", "ipc://")):
        raise ValueError("publisher endpoint must use TCP or IPC")
    if args.max_samples is not None and args.max_samples < 1:
        raise ValueError("max-samples must be positive")
    device, cameras, sources, stages, counters = load_settings(
        args.config, args.watch, args.camera_stage, args.camera_counter
    )
    collector = Collector(
        device,
        cameras,
        sources,
        stages,
        counters,
        max_age=args.max_age,
        buffer_source=args.buffer_source,
    )
    if (
        device.health_file.enabled
        and Path(device.health_file.path).resolve() == args.health_file.resolve()
    ):
        raise ValueError(
            "device health report and monitor process heartbeat must use different files"
        )
    publisher = Publisher(args.bind)
    stop, handlers = threading.Event(), {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    sequence, samples, sent, code = 0, 0, 0, 0
    stream_id = uuid.uuid4().hex
    next_sample = next_publish = next_file = time.monotonic()
    interval = args.publish_interval or device.heartbeat.interval_seconds
    health = None
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_sample:
                health = collector.collect()
                samples += 1
                atomic_json(
                    args.health_file,
                    {
                        "state": "running",
                        "processed": samples,
                        "published": sent,
                        "updated_at": time.time(),
                        "health": health,
                    },
                )
                log.info(
                    "device health",
                    extra={
                        "stage": "processing",
                        "event": "health_collected",
                        "fields": {
                            "device_id": device.id,
                            "status": health["status"],
                            "cameras_active": health["cameras_active"],
                            "cameras_error": health["cameras_error"],
                            "detections_total": sum(
                                c["detections_total"] for c in health["cameras"]
                            ),
                            "buffer_pending": health["buffer"]["pending"],
                        },
                    },
                )
                for name, service in health["services"].items():
                    log.info(
                        "service health",
                        extra={
                            "stage": "input",
                            "event": "service_health",
                            "fields": {
                                "service": name,
                                "status": service["status"],
                                **service.get("rates", {}),
                                "last_error": service["last_error"],
                            },
                        },
                    )
                next_sample = now + args.sample_interval
            if health is not None and now >= next_publish:
                sequence += 1
                publisher.publish(
                    {
                        "message_type": "health.v1",
                        "device_id": device.id,
                        "stream_id": stream_id,
                        "sequence": sequence,
                        "received_at_ns": time.time_ns(),
                        "health": health,
                    }
                )
                sent += 1
                log.info(
                    "health metadata published",
                    extra={
                        "stage": "output",
                        "event": "health_published",
                        "fields": {
                            "device_id": device.id,
                            "sent": sent,
                            "sequence": sequence,
                            "node_rows_enabled": device.heartbeat.enabled,
                        },
                    },
                )
                next_publish = now + interval
            if health is not None and device.health_file.enabled and now >= next_file:
                try:
                    atomic_json(Path(device.health_file.path), health)
                except OSError:
                    log.warning("configured device health file could not be written")
                next_file = now + device.health_file.interval_seconds
            if args.max_samples and samples >= args.max_samples:
                break
            stop.wait(
                min(0.1, max(0.001, min(next_sample, next_publish) - time.monotonic()))
            )
    except Exception:
        log.exception("monitor failed")
        code = 1
    finally:
        publisher.close()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        atomic_json(
            args.health_file,
            {
                "state": "failed" if code else "stopped",
                "processed": samples,
                "published": sent,
                "updated_at": time.time(),
                "health": health,
            },
        )
    return code


def main():
    args = parser().parse_args()
    configure(args, "monitor", args.instance_id)
    try:
        return run(args)
    except Exception as exc:
        log.error("monitor startup failed (%s): %s", type(exc).__name__, str(exc)[:200])
        return 1
