"""Inspect detection messages or save one annotated frame without a browser."""

import argparse
import json
from pathlib import Path

import zmq

from services.inference.messaging.detections import decode as decode_detections
from services.inference.messaging.frames import decode as decode_frame
from services.inference.runtime.logging import print_message


def receive():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument(
        "--kind", choices=("detections", "frames"), default="detections"
    )
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--format", choices=("console", "json"), default="console")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument(
        "--save",
        type=Path,
        help="overwrite this PNG/JPG with the latest annotated frame",
    )
    args = parser.parse_args()
    if args.count < 1 or not 0 < args.timeout < 3600:
        parser.error(
            "count must be positive; timeout must be between 0 and 3600 seconds"
        )
    kind = "detections.v1" if args.kind == "detections" else "frame.v1"
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
                if args.kind == "detections":
                    header = decode_detections(parts)
                else:
                    header, image = decode_frame(parts)
                if header["camera_id"] != args.camera_id:
                    continue
                if args.save and args.kind == "frames":
                    import cv2

                    if not cv2.imwrite(str(args.save), image):
                        parser.exit(1, "Could not save the frame.\n")
                if args.format == "json":
                    print(json.dumps(header), flush=True)
                else:
                    print_message(header, "inference", color=args.color)
                received += 1
    return 0


def main():
    try:
        return receive()
    except KeyboardInterrupt:
        # Context managers release the socket before exiting normally.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
