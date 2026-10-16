"""OpenCV source lifecycle, independent of inference and frame transport."""

from __future__ import annotations

import logging
import math
import os
import threading
import time

import cv2

log = logging.getLogger(__name__)
_FFMPEG_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
_FFMPEG_ENV_LOCK = threading.Lock()
_RTSP_OPTIONS = (
    "rtsp_transport;tcp|fflags;+discardcorrupt+genpts"
    "|probesize;50000000|analyzeduration;50000000"
    "|stimeout;5000000|timeout;5000000"
)


class SourceUnavailable(RuntimeError):
    """Opening or decoding failed, or a video file reached its end."""


class SourceStopped(RuntimeError):
    """Cooperative stop was requested; this source must not reconnect."""


def _open_capture(source: str | int):
    if isinstance(source, int):
        return cv2.VideoCapture(source)

    # OpenCV reads this process-global setting during capture construction.
    with _FFMPEG_ENV_LOCK:
        previous = os.environ.get(_FFMPEG_ENV)
        if source.lower().startswith(("rtsp://", "rtsps://")):
            os.environ[_FFMPEG_ENV] = _RTSP_OPTIONS
        else:
            os.environ.pop(_FFMPEG_ENV, None)
        try:
            return cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        finally:
            if previous is None:
                os.environ.pop(_FFMPEG_ENV, None)
            else:
                os.environ[_FFMPEG_ENV] = previous


class CameraSource:
    """One USB camera, stream, or video file; only request_stop is cross-thread safe."""

    def __init__(
        self,
        camera_id: str,
        source: str | int,
        *,
        probe_attempts: int = 120,
        probe_timeout: float = 30.0,
    ) -> None:
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("camera_id must be a nonempty string")
        if isinstance(source, bool) or not isinstance(source, (str, int)):
            raise ValueError("source must be a USB index, stream URL, or video path")
        if (isinstance(source, int) and source < 0) or (
            isinstance(source, str) and not source.strip()
        ):
            raise ValueError("source must not be empty or a negative USB index")
        if type(probe_attempts) is not int or probe_attempts < 1:
            raise ValueError("probe_attempts must be a positive integer")
        if not math.isfinite(probe_timeout) or probe_timeout <= 0:
            raise ValueError("probe_timeout must be finite and positive")
        self.camera_id = camera_id
        self.source = source
        self._probe_attempts = probe_attempts
        self._probe_timeout = probe_timeout
        self._cap = None
        self._first_frame = None
        self._size = (0, 0)
        self.received_at_ns = 0
        self._stop = threading.Event()
        self._owner = None

    def _check_owner(self) -> None:
        current = threading.current_thread()
        if self._owner is None:
            self._owner = current
        elif self._owner is not current:
            raise RuntimeError("capture operations must run on the owning thread")

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise SourceStopped(f"camera {self.camera_id}: stop requested")

    @property
    def frame_size(self) -> tuple[int, int]:
        """Decoded (width, height); (0, 0) while closed."""
        return self._size

    def open(self) -> None:
        self._check_owner()
        self._check_stop()
        if self._cap is not None:
            raise RuntimeError("camera source is already open; use reconnect()")
        try:
            self._cap = _open_capture(self.source)
            self._check_stop()
            if not self._cap.isOpened():
                raise SourceUnavailable(f"camera {self.camera_id}: cannot open source")
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Backend may ignore this.
            deadline = time.monotonic() + self._probe_timeout
            for _ in range(self._probe_attempts):
                self._check_stop()
                if time.monotonic() >= deadline:
                    break
                ok, frame = self._cap.read()
                received_at_ns = time.time_ns()
                self._check_stop()
                if ok and frame is not None and frame.size > 0:
                    self._first_frame = frame
                    self.received_at_ns = received_at_ns
                    self._size = (frame.shape[1], frame.shape[0])
                    log.info(
                        "camera %s: source opened (%s x %s)",
                        self.camera_id,
                        *self._size,
                    )
                    return
            raise SourceUnavailable(
                f"camera {self.camera_id}: no decoded frame during opening"
            )
        except BaseException as exc:
            self.close()
            if isinstance(exc, cv2.error):
                raise SourceUnavailable(
                    f"camera {self.camera_id}: source opening failed"
                ) from exc
            raise

    def read(self):
        """Return the next decoded BGR array, without inference or FPS gating."""
        self._check_owner()
        self._check_stop()
        if self._cap is None:
            raise RuntimeError("read() requires an open source")
        if self._first_frame is not None:
            frame, self._first_frame = self._first_frame, None
            return frame
        try:
            ok, frame = self._cap.read()
            received_at_ns = time.time_ns()
        except cv2.error as exc:
            raise SourceUnavailable(
                f"camera {self.camera_id}: frame decoding failed"
            ) from exc
        self._check_stop()
        if not ok or frame is None or frame.size == 0:
            raise SourceUnavailable(f"camera {self.camera_id}: no frame or end of file")
        self._size = (frame.shape[1], frame.shape[0])
        self.received_at_ns = received_at_ns
        return frame

    def request_stop(self) -> None:
        """Signal only; never release a capture another thread may be reading."""
        self._stop.set()

    def close(self) -> None:
        self._check_owner()
        cap, self._cap = self._cap, None
        self._first_frame = None
        self._size = (0, 0)
        self.received_at_ns = 0
        if cap is not None:
            try:
                cap.release()
            except cv2.error:
                log.warning("camera %s: capture release failed", self.camera_id)
            log.info("camera %s: source closed", self.camera_id)

    def reconnect(self) -> None:
        """Make one reconnect attempt; retry timing belongs to the capture loop."""
        self._check_owner()
        self.close()
        self._check_stop()
        log.info("camera %s: reconnecting", self.camera_id)
        self.open()
