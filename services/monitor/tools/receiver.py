"""Inspect monitor metadata with console or JSON formatting."""

import argparse
import json

import zmq

from ..messaging.health import decode, MAX_BATCH_BYTES
from ..runtime.logging import print_message


def receive():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    if args.count < 1 or not 0 < args.timeout < 3600:
        parser.error("count must be positive and timeout between 0 and 3600 seconds")
    with zmq.Context() as context:
        with context.socket(zmq.SUB) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVHWM, 4)
            socket.setsockopt(zmq.RCVTIMEO, int(args.timeout * 1000))
            socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
            socket.setsockopt(zmq.SUBSCRIBE, f"health.v1/{args.device_id}".encode())
            socket.connect(args.connect)
            count = 0
            while count < args.count:
                try:
                    batch = decode(socket.recv_multipart())
                except zmq.Again:
                    parser.exit(1, "Timed out waiting for monitor metadata.\n")
                if batch["device_id"] != args.device_id:
                    continue
                if args.format == "json":
                    print(json.dumps(batch), flush=True)
                else:
                    print_message(batch, "monitor", color=args.color)
                count += 1
    return 0


def main():
    try:
        return receive()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
