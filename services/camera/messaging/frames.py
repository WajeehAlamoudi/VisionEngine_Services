"""frame.v1 wire contract: topic, JSON metadata, contiguous BGR uint8 pixels."""

from dataclasses import dataclass
import json
import re

import numpy as np

MAX_FRAME_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Frame:
    camera_id: str
    stream_id: str
    sequence: int
    received_at_ns: int
    image: np.ndarray
    queued_at: float


def encode(frame: Frame) -> list[bytes]:
    image = np.ascontiguousarray(frame.image)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("frame.v1 requires a BGR uint8 image with three channels")
    if not 0 < image.nbytes <= MAX_FRAME_BYTES:
        raise ValueError("frame exceeds the frame.v1 size limit")
    header = {
        "message_type": "frame.v1",
        "camera_id": frame.camera_id,
        "stream_id": frame.stream_id,
        "sequence": frame.sequence,
        "received_at_ns": frame.received_at_ns,
        "timestamp_origin": "host_decode",
        "encoding": "raw",
        "pixel_format": "bgr8",
        "width": image.shape[1],
        "height": image.shape[0],
        "payload_bytes": image.nbytes,
    }
    return [
        f"frame.v1/{frame.camera_id}".encode(),
        json.dumps(header, separators=(",", ":")).encode(),
        image.tobytes(),
    ]


def decode(parts: list[bytes]) -> tuple[dict, np.ndarray]:
    """Validate before reshaping; never deserialize Python objects from the network."""
    if len(parts) != 3 or len(parts[1]) > 16_384:
        raise ValueError("expected topic, small JSON header, and pixel payload")
    try:
        header = json.loads(parts[1])
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid frame header") from exc
    if not isinstance(header, dict):
        raise ValueError("frame header must be a mapping")
    if any(
        header.get(key) != value
        for key, value in {
            "message_type": "frame.v1",
            "encoding": "raw",
            "pixel_format": "bgr8",
            "timestamp_origin": "host_decode",
        }.items()
    ):
        raise ValueError("unsupported frame contract")
    camera_id = header.get("camera_id")
    if not isinstance(camera_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", camera_id):
        raise ValueError("invalid camera ID")
    if parts[0] != f"frame.v1/{camera_id}".encode():
        raise ValueError("frame topic and camera ID differ")
    if not isinstance(header.get("stream_id"), str) or not header["stream_id"]:
        raise ValueError("missing stream ID")
    for key in ("width", "height", "sequence", "received_at_ns", "payload_bytes"):
        if type(header.get(key)) is not int or header[key] < 1:
            raise ValueError(f"invalid {key}")
    expected = header["width"] * header["height"] * 3
    if (
        expected > MAX_FRAME_BYTES
        or expected != header["payload_bytes"]
        or expected != len(parts[2])
    ):
        raise ValueError("pixel payload size does not match dimensions")
    return header, np.frombuffer(parts[2], dtype=np.uint8).reshape(
        header["height"], header["width"], 3
    )
