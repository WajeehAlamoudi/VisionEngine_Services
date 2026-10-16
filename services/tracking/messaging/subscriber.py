"""Receive this camera's detections and optional original frames."""

import logging
import threading

import zmq

from .detections import decode as decode_detections
from .frames import decode as decode_frame

log = logging.getLogger(__name__)


class Subscriber(threading.Thread):
    def __init__(self, camera_id, model_id, endpoint, frames_endpoint, inbox, stop):
        super().__init__(name="tracking-input", daemon=True)
        self.camera_id, self.model_id = camera_id, model_id
        self.endpoint, self.frames_endpoint = endpoint, frames_endpoint
        self.inbox, self.stop, self.error = inbox, stop, None

    def run(self):
        sockets = {}
        try:
            with zmq.Context() as context:
                try:
                    poller = zmq.Poller()
                    for kind, endpoint, limit in (
                        ("detections", self.endpoint, self.inbox.max_items),
                        ("frames", self.frames_endpoint, self.inbox.frame_max_items),
                    ):
                        if not endpoint:
                            continue
                        socket = context.socket(zmq.SUB)
                        sockets[socket] = kind
                        socket.setsockopt(zmq.LINGER, 0)
                        socket.setsockopt(zmq.RCVHWM, limit)
                        socket.setsockopt(zmq.MAXMSGSIZE, 64 * 1024 * 1024)
                        topic = "frame.v1" if kind == "frames" else "detections.v1"
                        socket.setsockopt(
                            zmq.SUBSCRIBE, f"{topic}/{self.camera_id}".encode()
                        )
                        socket.connect(endpoint)
                        poller.register(socket, zmq.POLLIN)
                    while not self.stop.is_set():
                        for socket, _event in poller.poll(100):
                            parts = socket.recv_multipart()
                            try:
                                if sockets[socket] == "frames":
                                    header, image = decode_frame(parts)
                                    if header["camera_id"] == self.camera_id:
                                        self.inbox.put_frame(header, image)
                                else:
                                    batch = decode_detections(parts)
                                    if (
                                        batch["camera_id"] == self.camera_id
                                        and batch["model_id"] == self.model_id
                                    ):
                                        self.inbox.put_batch(batch, len(parts[1]))
                            except (ValueError, TypeError, KeyError, UnicodeError):
                                self.inbox.reject()
                                log.warning(
                                    "invalid input discarded",
                                    extra={"stage": "input", "event": "invalid_input"},
                                )
                finally:
                    for socket in sockets:
                        socket.close(linger=0)
        except Exception as exc:
            self.error = exc
            log.exception("tracking receiver failed")
            self.stop.set()
