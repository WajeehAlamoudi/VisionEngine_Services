"""Standalone live notifier: bounded input, independent channels and health snapshots."""

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import time

import httpx
import zmq
import zmq.asyncio

from .configuration import load_settings
from .logging import configure, add_arguments
from ..messaging.alerts import decode, MAX_BATCH_BYTES
from ..messaging.publisher import Publisher
from ..processing.queue import Inbox
from ..processing.worker import Dispatcher

log = logging.getLogger("notifier")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("config/YAMLs"))
    p.add_argument("--instance-id", default="notifier")
    p.add_argument("--alerts-input", action="append", required=True)
    p.add_argument("--bind", default="tcp://0.0.0.0:5567")
    p.add_argument("--max-items", type=positive, default=32)
    p.add_argument("--max-bytes", type=positive, default=4194304)
    p.add_argument("--max-age-ms", type=int, default=5000)
    p.add_argument("--delivery-max-items", type=positive, default=256)
    p.add_argument("--delivery-max-bytes", type=positive, default=4194304)
    p.add_argument("--delivery-max-age-ms", type=positive, default=30000)
    p.add_argument("--shutdown-timeout", type=positive, default=10)
    p.add_argument("--max-batches", type=positive)
    p.add_argument(
        "--health-file",
        type=Path,
        default=Path(
            os.environ.get("NOTIFIER_HEALTH_FILE", "/tmp/notifier-health.json")
        ),
    )
    add_arguments(p)
    return p


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


def log_status(status):
    log.info(
        "alert receiver status",
        extra={
            "stage": "input",
            "event": "receiver_status",
            "fields": status["inputs"],
        },
    )
    log.info(
        "notification dispatch status",
        extra={
            "stage": "processing",
            "event": "dispatch_status",
            "fields": {
                "state": status["state"],
                "processed": status["processed"],
                "alerts": status["alerts_total"],
                "duplicates": status["duplicate_batches"],
            },
        },
    )
    for name, channel in status["channels"].items():
        log.info(
            "notification channel status",
            extra={
                "stage": "output",
                "event": "channel_status",
                "fields": {"channel": name, **channel},
            },
        )


async def run(args):
    if args.max_age_ms < 0 or len(set(args.alerts_input)) != len(args.alerts_input):
        raise ValueError("invalid input limits or duplicate endpoint")
    if (
        args.max_items > 1000
        or args.delivery_max_items > 10000
        or max(args.max_bytes, args.delivery_max_bytes) > MAX_BATCH_BYTES
    ):
        raise ValueError("queue bounds exceed supported limits")
    for endpoint in [*args.alerts_input, args.bind]:
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("expected TCP or IPC endpoint")
    settings, api, device, passwords = load_settings(args.config)
    stop = asyncio.Event()
    closing = asyncio.Event()
    handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    context = zmq.asyncio.Context()
    sockets = []
    publisher = None
    receiver_task = None
    delivery_tasks = []
    state = "loading"
    started = time.monotonic()
    error = None
    inbox = Inbox(args.max_items, args.max_bytes, args.max_age_ms)
    client = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )
    dispatcher = Dispatcher(
        settings,
        api,
        device,
        client,
        passwords,
        max_items=args.delivery_max_items,
        max_bytes=args.delivery_max_bytes,
        max_age_ms=args.delivery_max_age_ms,
    )

    def snapshot():
        now = time.time_ns()
        return {
            **dispatcher.snapshot(),
            "state": state,
            "since": started,
            "processed_at_ns": now,
            "heartbeat_at_ns": now,
            "max_work_age_seconds": 30,
            "inputs": inbox.snapshot(),
        }

    async def receive():
        poller = zmq.asyncio.Poller()
        for socket in sockets:
            poller.register(socket, zmq.POLLIN)
        while not stop.is_set():
            ready = await poller.poll(100)
            for socket, _ in ready:
                parts = await socket.recv_multipart()
                try:
                    batch = decode(parts)
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    inbox.rejected += 1
                    continue
                inbox.put(batch, len(parts[1]))

    try:
        publisher = Publisher(args.bind)
        for endpoint in args.alerts_input:
            socket = context.socket(zmq.SUB)
            sockets.append(socket)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVHWM, args.max_items)
            socket.setsockopt(zmq.MAXMSGSIZE, min(args.max_bytes, MAX_BATCH_BYTES))
            socket.setsockopt(zmq.SUBSCRIBE, b"alert.v1/")
            socket.connect(endpoint)
        receiver_task = asyncio.create_task(receive())
        delivery_tasks = [
            asyncio.create_task(dispatcher.deliver(c, closing))
            for c in dispatcher.channels.values()
        ]
        next_status = 0
        state = "waiting"
        while not stop.is_set():
            if receiver_task.done():
                receiver_task.result()
                raise RuntimeError("alert receiver stopped")
            for task in delivery_tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("delivery worker stopped")
            batch = inbox.pop()
            if batch is not None:
                state = "dispatching"
                dispatcher.submit(batch)
                state = "waiting"
            if time.monotonic() >= next_status:
                status = snapshot()
                write_status(args.health_file, status)
                publisher.publish(device.id, status)
                log_status(status)
                next_status = time.monotonic() + 5
            if args.max_batches and dispatcher.processed >= args.max_batches:
                break
            await asyncio.sleep(0.01)
    except Exception as exc:
        error = exc
        log.error("notifier failed (%s)", type(exc).__name__)
    finally:
        stop.set()
        if receiver_task:
            receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver_task
        inbox.discard_all()
        closing.set()
        if delivery_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*delivery_tasks), args.shutdown_timeout
                )
            except Exception:
                for task in delivery_tasks:
                    task.cancel()
                await asyncio.gather(*delivery_tasks, return_exceptions=True)
        for channel in dispatcher.channels.values():
            channel["inbox"].discard_all()
        state = "failed" if error else "stopped"
        write_status(args.health_file, snapshot())
        if publisher:
            publisher.close()
        for socket in sockets:
            socket.close(linger=0)
        context.term()
        await client.aclose()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    return int(error is not None)


def main():
    args = parser().parse_args()
    configure(args, "notifier", args.instance_id)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log.error("notifier startup failed (%s)", type(exc).__name__)
        return 1
