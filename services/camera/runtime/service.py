"""Run one camera: configuration -> capture -> publisher."""

import argparse
import json
import logging
import math
import os
import queue
from pathlib import Path
import signal
import threading
import time

import zmq

from config.controller.parsers.strict import ConfigError
from .configuration import load_camera
from .logging import add_arguments, configure, logging_dropped_total
from ..messaging.publisher import Publisher
from ..capture.source import CameraSource
from ..capture.worker import CameraWorker, FrameBuffer

log = logging.getLogger("camera")


def positive_number(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def positive_int(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def nonnegative_int(value):
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        type=Path,
        default=Path("config/YAMLs"),
        help="directory containing cameras.yaml",
    )
    result.add_argument("--camera-id", required=True, help="one enabled camera ID")
    result.add_argument(
        "--pipeline",
        type=Path,
        help="optionally require the camera node in a generated pipeline",
    )
    result.add_argument(
        "--bind", default="tcp://0.0.0.0:5555", help="frame output endpoint"
    )
    result.add_argument("--max-items", type=positive_int, default=2)
    result.add_argument("--max-bytes", type=positive_int, default=16_777_216)
    result.add_argument(
        "--max-age-ms",
        type=nonnegative_int,
        default=500,
        help="local queue age limit; zero disables expiry",
    )
    result.add_argument(
        "--overflow",
        choices=("drop_oldest", "drop_newest", "block"),
        default="drop_oldest",
    )
    result.add_argument(
        "--transport-hwm",
        type=positive_int,
        default=2,
        help="ZeroMQ per-subscriber message high-water mark",
    )
    result.add_argument("--file-mode", choices=("stop", "loop"), default="stop")
    result.add_argument(
        "--file-fps",
        type=positive_number,
        default=30,
        help="video file capture pace; publication uses cameras.yaml fps_target",
    )
    result.add_argument(
        "--native-timeout",
        type=positive_number,
        default=90,
        help="process watchdog deadline for open/read/close",
    )
    result.add_argument("--shutdown-timeout", type=positive_number, default=10)
    result.add_argument(
        "--max-frames",
        type=positive_int,
        help="stop after this many publish calls (diagnostics)",
    )
    result.add_argument(
        "--health-file", type=Path, default=os.environ.get("CAMERA_HEALTH_FILE")
    )
    add_arguments(result)
    return result


def write_status(path: Path, status: dict):
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(status), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        log.warning("could not update camera health file")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class Telemetry(threading.Thread):
    """Latest-only status persistence; never execute disk I/O on the frame loop."""

    def __init__(self, path):
        super().__init__(name="camera-health-writer", daemon=True)
        self.path = path
        self.pending = queue.Queue(1)
        self.stopping = threading.Event()
        self.dropped = 0

    def submit(self, status):
        try:
            self.pending.put_nowait(status)
        except queue.Full:
            try:
                self.pending.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            self.pending.put_nowait(status)

    def run(self):
        last_write_ms = 0.0
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                status = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            final = status.pop("final", False)
            status["telemetry_dropped_total"] = self.dropped + logging_dropped_total()
            status["health_write_duration_ms_max"] = last_write_ms
            if self.path:
                started = time.perf_counter()
                write_status(self.path, status)
                last_write_ms = (time.perf_counter() - started) * 1000
            status["health_write_duration_ms_max"] = last_write_ms
            if not final:
                log_status(status)


def log_status(status):
    log.info(
        "capture status",
        extra={
            "stage": "input",
            "event": "capture_status",
            "fields": {
                "state": status["state"],
                "captured_total": status["captured"],
                "fps": status["capture_fps"],
                "reconnects_total": status["reconnects"],
                "sample_elapsed_ms": status["sample_elapsed_ms"],
                "sampled_at": status["updated_at"],
            },
        },
    )
    log.info(
        "publisher status",
        extra={
            "stage": "output",
            "event": "publisher_status",
            "fields": {
                "sent_total": status["published"],
                "sampled_at": status["updated_at"],
                "fps": status["publish_fps"],
                "fps_target": status["fps_target"],
                "rate_limited_total": status["rate_limited_total"],
                "queued_frames": status["queued"],
                "queued_bytes": status["queued_bytes"],
                "dropped_total": status["dropped_local"],
                **{
                    key: status[key]
                    for key in (
                        "sample_elapsed_ms",
                        "publish_loop_gap_ms_max",
                        "frame_queue_age_ms_max",
                        "publish_duration_ms_max",
                        "health_write_duration_ms_max",
                        "frames_dropped_overflow_total",
                        "frames_dropped_expired_total",
                        "telemetry_dropped_total",
                    )
                },
            },
        },
    )


def run(args) -> tuple[int, bool]:
    """Return exit code and whether a stuck native thread requires immediate exit."""
    camera = load_camera(args.config, args.camera_id, args.pipeline)
    buffer = FrameBuffer(args.max_items, args.max_bytes, args.overflow, args.max_age_ms)
    source = CameraSource(camera.id, camera.source)
    worker = CameraWorker(
        source, buffer, file_mode=args.file_mode, file_fps=args.file_fps
    )
    publisher = Publisher(args.bind, high_water_mark=args.transport_hwm)
    stop = threading.Event()
    old_handlers = {}

    def on_stop(signum, frame):
        stop.set()
        worker.request_stop()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, on_stop)
    log.info(
        "camera %s: frames output bound to %s",
        camera.id,
        publisher.endpoint,
        extra={
            "stage": "output",
            "event": "publisher_bound",
            "fields": {"fps_target": camera.fps_target},
        },
    )
    worker.start()
    telemetry = Telemetry(args.health_file)
    telemetry.start()
    last_loop = time.perf_counter()
    metrics = dict.fromkeys(
        (
            "publish_loop_gap_ms_max",
            "frame_queue_age_ms_max",
            "publish_duration_ms_max",
        ),
        0.0,
    )
    next_status = 0
    previous_status_time = time.monotonic()
    previous_captured = previous_published = 0
    publish_interval = 1 / camera.fps_target
    next_publish = 0.0
    rate_limited_total = 0
    code = 0
    try:
        while not stop.is_set():
            if not telemetry.is_alive():
                log.error("camera telemetry worker stopped unexpectedly")
                code = 1
                break
            now = time.perf_counter()
            metrics["publish_loop_gap_ms_max"] = max(
                metrics["publish_loop_gap_ms_max"], (now - last_loop) * 1000
            )
            last_loop = now
            snapshot = worker.snapshot()
            if (
                snapshot["state"] in ("opening", "reading", "closing")
                and time.monotonic() - snapshot["since"] > args.native_timeout
            ):
                log.error(
                    "camera %s: native operation timed out; process restart required",
                    camera.id,
                )
                code = 1
                break
            frame = None
            wait = next_publish - time.monotonic()
            if wait > 0:
                stop.wait(min(wait, 0.1))
            else:
                frame = buffer.get()
                if frame is not None:
                    # Select the newest queued frame at this publication deadline.
                    # Capture, queue expiry and reconnect clearing stay independent.
                    for _ in range(args.max_items - 1):
                        newer = buffer.get(timeout=0)
                        if newer is None:
                            break
                        frame = newer
                        rate_limited_total += 1
            if frame is not None:
                now = time.monotonic()
                following = next_publish + publish_interval
                # Preserve phase over small delays; do not replay missed deadlines.
                next_publish = (
                    following
                    if next_publish and following > now
                    else now + publish_interval
                )
                started = time.perf_counter()
                metrics["frame_queue_age_ms_max"] = max(
                    metrics["frame_queue_age_ms_max"],
                    (time.monotonic() - frame.queued_at) * 1000,
                )
                publisher.publish(frame)
                metrics["publish_duration_ms_max"] = max(
                    metrics["publish_duration_ms_max"],
                    (time.perf_counter() - started) * 1000,
                )
                log.debug(
                    "frame sent",
                    extra={
                        "stage": "output",
                        "event": "frame_sent",
                        "fields": {
                            "camera_id": frame.camera_id,
                            "stream_id": frame.stream_id,
                            "sequence": frame.sequence,
                            "payload_bytes": frame.image.nbytes,
                        },
                    },
                )
            if time.monotonic() >= next_status:
                status = {
                    **worker.snapshot(),
                    **buffer.snapshot(),
                    "published": publisher.sent,
                    "fps_target": camera.fps_target,
                    "rate_limited_total": rate_limited_total,
                    "updated_at": time.time(),
                }
                elapsed = max(time.monotonic() - previous_status_time, 0.001)
                status["capture_fps"] = round(
                    (status["captured"] - previous_captured) / elapsed, 2
                )
                status["publish_fps"] = round(
                    (publisher.sent - previous_published) / elapsed, 2
                )
                previous_captured, previous_published = (
                    status["captured"],
                    publisher.sent,
                )
                previous_status_time = time.monotonic()
                status.update(metrics, sample_elapsed_ms=elapsed * 1000)
                metrics = dict.fromkeys(metrics, 0.0)
                telemetry.submit(status)
                next_status = time.monotonic() + 5
            if args.max_frames and publisher.sent >= args.max_frames:
                break
            if worker.done.is_set() and buffer.snapshot()["queued"] == 0:
                code = 1 if worker.error else 0
                break
    finally:
        worker.request_stop()
        worker.join(args.shutdown_timeout)
        publisher.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        telemetry.submit(
            {
                **worker.snapshot(),
                **buffer.snapshot(),
                "published": publisher.sent,
                "fps_target": camera.fps_target,
                "rate_limited_total": rate_limited_total,
                "state": "failed" if code or worker.is_alive() else "stopped",
                "updated_at": time.time(),
                "final": True,
            }
        )
        telemetry.stopping.set()
        telemetry.join(args.shutdown_timeout)
        log.info(
            "camera %s: stopped; captured=%s published=%s dropped_local=%s rate_limited=%s",
            camera.id,
            worker.snapshot()["captured"],
            publisher.sent,
            buffer.snapshot()["dropped_local"],
            rate_limited_total,
        )
    return code, worker.is_alive() or telemetry.is_alive()


def main():
    args = parser().parse_args()
    configure(
        args,
        "camera",
        "camera-" + args.camera_id.replace("_", "-").lower(),
        camera_id=args.camera_id,
    )
    try:
        code, stuck = run(args)
    except (ConfigError, ValueError, OSError, zmq.ZMQError) as exc:
        log.error(
            "camera could not run (%s); check configuration and output binding",
            type(exc).__name__,
        )
        return 1
    if stuck:
        log.error("camera worker did not stop; exiting for supervisor recovery")
        os._exit(1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
