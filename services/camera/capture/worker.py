"""One capture owner, bounded buffering, retry scheduling, and lifecycle status."""

from collections import deque
import logging
import threading
import time
import uuid

from ..messaging.frames import Frame
from .source import CameraSource, SourceStopped, SourceUnavailable

log = logging.getLogger(__name__)


class FrameBuffer:
    """Bound the local capture-to-publisher queue by both item count and bytes."""

    def __init__(
        self, max_items=2, max_bytes=16_777_216, overflow="drop_oldest", max_age_ms=500
    ):
        if max_items < 1 or max_bytes < 1 or max_age_ms < 0:
            raise ValueError(
                "buffer limits must be positive; age can be zero (disabled)"
            )
        if overflow not in ("drop_oldest", "drop_newest", "block"):
            raise ValueError("unsupported buffer overflow policy")
        self.max_items, self.max_bytes = max_items, max_bytes
        self.overflow, self.max_age = overflow, max_age_ms / 1000
        self._items = deque()
        self._bytes = 0
        self._condition = threading.Condition()
        self.dropped = 0

    def _remove(self):
        frame = self._items.popleft()
        self._bytes -= frame.image.nbytes
        return frame

    def _expire(self):
        while (
            self._items
            and self.max_age
            and time.monotonic() - self._items[0].queued_at > self.max_age
        ):
            self._remove()
            self.dropped += 1

    def put(self, frame: Frame, stop: threading.Event) -> bool:
        with self._condition:
            if frame.image.nbytes > self.max_bytes:
                self.dropped += 1
                return False
            while not stop.is_set():
                self._expire()
                full = (
                    len(self._items) >= self.max_items
                    or self._bytes + frame.image.nbytes > self.max_bytes
                )
                if not full:
                    if (
                        self.max_age
                        and time.monotonic() - frame.queued_at > self.max_age
                    ):
                        self.dropped += 1
                        return False
                    self._items.append(frame)
                    self._bytes += frame.image.nbytes
                    self._condition.notify_all()
                    return True
                if self.overflow == "drop_newest":
                    self.dropped += 1
                    return False
                if self.overflow == "drop_oldest":
                    self._remove()
                    self.dropped += 1
                else:
                    self._condition.wait(0.1)
            return False

    def get(self, timeout=0.1):
        with self._condition:
            self._expire()
            if not self._items:
                self._condition.wait(timeout)
                self._expire()
            result = self._remove() if self._items else None
            self._condition.notify_all()
            return result

    def clear(self):
        with self._condition:
            self.dropped += len(self._items)
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {
                "queued": len(self._items),
                "queued_bytes": self._bytes,
                "dropped_local": self.dropped,
            }


class CameraWorker(threading.Thread):
    def __init__(
        self,
        source: CameraSource,
        buffer: FrameBuffer,
        *,
        file_mode="stop",
        file_fps=30.0,
    ):
        super().__init__(name=f"camera-{source.camera_id}", daemon=True)
        self.source, self.buffer = source, buffer
        self.file_mode, self.file_interval = file_mode, 1 / file_fps
        self.is_file = isinstance(source.source, str) and "://" not in source.source
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self.stream_id = uuid.uuid4().hex
        self.error = None
        self._lock = threading.Lock()
        self._status = {
            "state": "created",
            "since": time.monotonic(),
            "captured": 0,
            "reconnects": 0,
        }

    def _state(self, state):
        with self._lock:
            previous = self._status["state"]
            self._status.update(state=state, since=time.monotonic())
        if (
            state in ("opening", "retrying", "completed", "stopped", "failed")
            and state != previous
        ):
            log.info(
                "capture state changed",
                extra={
                    "stage": "input",
                    "event": "capture_state",
                    "fields": {"camera_id": self.source.camera_id, "state": state},
                },
            )

    def snapshot(self):
        with self._lock:
            return {
                "camera_id": self.source.camera_id,
                "stream_id": self.stream_id,
                **self._status,
            }

    def request_stop(self):
        self.stop_event.set()
        self.source.request_stop()

    def run(self):
        delay = 2
        sequence = 0
        opened = False
        next_file_frame = time.monotonic()
        try:
            while not self.stop_event.is_set():
                try:
                    if not opened:
                        self._state("opening")
                        self.source.open()
                        opened = True
                        next_file_frame = time.monotonic()
                    self._state("reading")
                    image = self.source.read()
                    sequence += 1
                    delay = 2
                    with self._lock:
                        self._status["captured"] += 1
                        self._status["received_at_ns"] = self.source.received_at_ns
                    log.debug(
                        "frame captured",
                        extra={
                            "stage": "input",
                            "event": "frame_captured",
                            "fields": {
                                "camera_id": self.source.camera_id,
                                "stream_id": self.stream_id,
                                "sequence": sequence,
                                "width": image.shape[1],
                                "height": image.shape[0],
                            },
                        },
                    )
                    self._state("buffering")
                    self.buffer.put(
                        Frame(
                            self.source.camera_id,
                            self.stream_id,
                            sequence,
                            self.source.received_at_ns,
                            image,
                            time.monotonic(),
                        ),
                        self.stop_event,
                    )
                    if self.is_file:
                        self._state("pacing")
                        next_file_frame += self.file_interval
                        self.stop_event.wait(max(0, next_file_frame - time.monotonic()))
                except SourceUnavailable:
                    was_open = opened
                    self._state("closing")
                    self.source.close()
                    opened = False
                    if self.is_file and was_open and self.file_mode == "stop":
                        self._state("completed")
                        break
                    self.buffer.clear()  # Do not send pre-disconnect frames after recovery.
                    with self._lock:
                        self._status["reconnects"] += 1
                    self._state("retrying")
                    log.warning(
                        "camera %s: source unavailable; retry in %ss",
                        self.source.camera_id,
                        delay,
                    )
                    if self.stop_event.wait(delay):
                        break
                    delay = min(delay * 2, 60)
        except SourceStopped:
            pass
        except Exception as exc:
            # Avoid printing exception strings that may contain RTSP credentials.
            self.error = type(exc).__name__
            log.error(
                "camera %s: worker failed (%s)", self.source.camera_id, self.error
            )
        finally:
            completed = self.snapshot()["state"] == "completed"
            self._state("closing")
            try:
                self.source.close()
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                self._state(
                    "failed" if self.error else "completed" if completed else "stopped"
                )
                self.done.set()
