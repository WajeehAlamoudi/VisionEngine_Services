"""Diagnostic frame consumer: receive pixels, validate them, optionally save a PNG."""

import argparse
import json
from pathlib import Path
import time

import cv2
import zmq

from ..messaging.frames import MAX_FRAME_BYTES, decode
from ..runtime.service import positive_int, positive_number
from ..runtime.logging import print_message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", default="tcp://127.0.0.1:5555")
    parser.add_argument("--camera-id", help="optional exact camera filter")
    parser.add_argument("--count", type=positive_int, default=10)
    parser.add_argument(
        "--timeout",
        type=positive_number,
        default=30,
        help="total receive deadline in seconds",
    )
    parser.add_argument(
        "--save", type=Path, help="write the last received frame as a PNG"
    )
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.RCVHWM, 2)
    socket.setsockopt(zmq.MAXMSGSIZE, MAX_FRAME_BYTES)
    socket.setsockopt(
        zmq.SUBSCRIBE,
        (f"frame.v1/{args.camera_id}" if args.camera_id else "frame.v1/").encode(),
    )
    deadline = time.monotonic() + args.timeout
    received = 0
    image = None
    try:
        socket.connect(args.connect)
        while received < args.count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not socket.poll(max(1, int(remaining * 1000))):
                print(f"Timed out: received {received}/{args.count} frames.")
                return 1
            header, decoded = decode(socket.recv_multipart())
            # ZeroMQ topics match prefixes, so check the exact ID too.
            if args.camera_id and header["camera_id"] != args.camera_id:
                continue
            image = decoded
            received += 1
            if args.format == "json":
                print(json.dumps(header), flush=True)
            else:
                print_message(header, "camera", color=args.color)
        if args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(args.save), image):
                raise ValueError("could not save received frame")
        return 0
    except (ValueError, OSError, zmq.ZMQError, cv2.error) as exc:
        print(f"Receiver failed ({type(exc).__name__}).")
        return 1
    finally:
        socket.close()
        context.term()


if __name__ == "__main__":
    raise SystemExit(main())
