"""Observe buffer status publications; row ownership stays on the RPC interface."""

import argparse
import json

import zmq

from ..runtime.logging import print_message


def receive():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("count must be positive")
    with zmq.Context() as context:
        with context.socket(zmq.SUB) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVTIMEO, 30000)
            socket.setsockopt(zmq.RCVHWM, 4)
            socket.setsockopt(zmq.MAXMSGSIZE, 1024 * 1024)
            topic = f"buffer.status.v1/{args.device_id}".encode()
            socket.setsockopt(zmq.SUBSCRIBE, topic)
            socket.connect(args.connect)
            count = 0
            while count < args.count:
                try:
                    parts = socket.recv_multipart()
                except zmq.Again:
                    parser.exit(1, "Timed out waiting for buffer status.\n")
                if len(parts) != 2 or parts[0] != topic:
                    continue
                batch = json.loads(parts[1])
                if args.format == "json":
                    print(json.dumps(batch), flush=True)
                else:
                    print_message(batch, "buffer", color=args.color)
                count += 1
    return 0


def main():
    try:
        return receive()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
