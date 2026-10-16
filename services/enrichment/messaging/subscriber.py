"""Receive detection batches from tracking or directly from inference."""

import logging
import threading

import zmq

from .detections import MAX_BATCH_BYTES, decode

log = logging.getLogger(__name__)


class Subscriber(threading.Thread):
    def __init__(self, camera, model, endpoint, inbox, stop):
        super().__init__(name="enrichment-input", daemon=True)
        self.camera, self.model, self.endpoint = camera, model, endpoint
        self.inbox, self.stop, self.error = inbox, stop, None

    def run(self):
        try:
            with zmq.Context() as context:
                with context.socket(zmq.SUB) as socket:
                    socket.setsockopt(zmq.LINGER, 0)
                    socket.setsockopt(zmq.RCVHWM, self.inbox.max_items)
                    socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
                    socket.setsockopt(
                        zmq.SUBSCRIBE, f"detections.v1/{self.camera.id}".encode()
                    )
                    socket.connect(self.endpoint)
                    while not self.stop.is_set():
                        if not socket.poll(100):
                            continue
                        parts = socket.recv_multipart()
                        try:
                            batch = decode(parts)
                            if (
                                batch["camera_id"] != self.camera.id
                                or batch["model_id"] != self.model.id
                            ):
                                continue
                            for detection in batch["detections"]:
                                if detection[
                                    "class_name"
                                ] not in self.model.classes or not isinstance(
                                    detection.get("attributes", {}), dict
                                ):
                                    raise ValueError(
                                        "invalid detection class or attributes"
                                    )
                            self.inbox.put(batch, len(parts[1]))
                        except (ValueError, TypeError, KeyError, UnicodeError):
                            self.inbox.reject()
                            log.warning(
                                "invalid input discarded",
                                extra={"stage": "input", "event": "invalid_input"},
                            )
        except Exception as exc:
            self.error = exc
            log.exception("enrichment receiver failed")
            self.stop.set()
