"""Inspect the row publisher using console formatting or JSON."""

import argparse
import json

import zmq

from ..messaging.rows import decode, MAX_BATCH_BYTES
from ..runtime.logging import print_message


def receive():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--camera-id")
    identity.add_argument("--device-id")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    if args.count < 1 or not 0 < args.timeout < 3600:
        parser.error(
            "count must be positive; timeout must be between 0 and 3600 seconds"
        )
    with zmq.Context() as context:
        with context.socket(zmq.SUB) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVHWM, 32)
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
            socket.setsockopt(zmq.RCVTIMEO, int(args.timeout * 1000))
            topic = (
                f"rows.v1/device/{args.device_id}"
                if args.device_id
                else f"rows.v1/{args.camera_id}"
            )
            socket.setsockopt(zmq.SUBSCRIBE, topic.encode())
            socket.connect(args.connect)
            received = 0
            while received < args.count:
                try:
                    batch = decode(socket.recv_multipart())
                except zmq.Again:
                    parser.exit(1, "Timed out waiting for rows.\n")
                if args.device_id and batch.get("device_id") != args.device_id:
                    continue
                if args.camera_id and batch.get("camera_id") != args.camera_id:
                    continue
                if args.format == "json":
                    print(json.dumps(batch), flush=True)
                else:
                    print_message(batch, "row-builder", color=args.color)
                received += 1
    return 0


def main():
    try:
        return receive()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
