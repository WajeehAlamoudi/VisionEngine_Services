"""Inspect published enrichment results without a browser."""

import argparse
import json

import zmq

from services.enrichment.messaging.events import decode as decode_events
from services.enrichment.runtime.logging import print_message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    if args.count < 1 or not 0 < args.timeout < 3600:
        parser.error(
            "count must be positive; timeout must be between 0 and 3600 seconds"
        )
    kind = "event.v1"
    with zmq.Context() as context:
        with context.socket(zmq.SUB) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVHWM, 2)
            socket.setsockopt(zmq.RCVTIMEO, int(args.timeout * 1000))
            socket.setsockopt(zmq.MAXMSGSIZE, 64 * 1024 * 1024)
            socket.setsockopt(zmq.SUBSCRIBE, f"{kind}/{args.camera_id}".encode())
            socket.connect(args.connect)
            received = 0
            while received < args.count:
                try:
                    parts = socket.recv_multipart()
                except zmq.Again:
                    parser.exit(1, "Timed out waiting for output.\n")
                header = decode_events(parts)
                if header["camera_id"] != args.camera_id:
                    continue
                if args.format == "json":
                    print(json.dumps(header), flush=True)
                else:
                    print_message(header, "enrichment", color=args.color)
                received += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
