"""One input socket per configured enrichment publisher; exact camera/model checks."""

import logging
import threading

import zmq

from .events import decode, MAX_BATCH_BYTES

log = logging.getLogger(__name__)


class Subscriber(threading.Thread):
    def __init__(self, inputs, inbox, stop):
        super().__init__(name="rules-input", daemon=True)
        self.inputs, self.inbox, self.stop, self.error = inputs, inbox, stop, None

    def run(self):
        sockets = {}
        try:
            with zmq.Context() as context:
                try:
                    poller = zmq.Poller()
                    for key, source in self.inputs.items():
                        socket = context.socket(zmq.SUB)
                        sockets[socket] = key
                        socket.setsockopt(zmq.LINGER, 0)
                        socket.setsockopt(zmq.RCVHWM, self.inbox.max_items)
                        socket.setsockopt(zmq.MAXMSGSIZE, MAX_BATCH_BYTES)
                        socket.setsockopt(zmq.SUBSCRIBE, f"event.v1/{key}".encode())
                        socket.connect(source.endpoint)
                        poller.register(socket, zmq.POLLIN)
                    while not self.stop.is_set():
                        for socket, _event in poller.poll(100):
                            key = sockets[socket]
                            parts = socket.recv_multipart()
                            try:
                                batch = decode(parts)
                                if (
                                    batch["camera_id"] != key
                                    or batch["model_id"]
                                    != self.inputs[key].camera.model_id
                                ):
                                    continue
                                for event in batch["events"]:
                                    if (
                                        event["class_name"]
                                        not in self.inputs[key].model.classes
                                    ):
                                        raise ValueError(
                                            "event class is outside configured model"
                                        )
                                self.inbox.put(batch, len(parts[1]))
                            except (ValueError, TypeError, KeyError, UnicodeError):
                                self.inbox.reject(key)
                                log.warning(
                                    "invalid event batch discarded",
                                    extra={
                                        "stage": "input",
                                        "event": "invalid_input",
                                        "fields": {"camera_id": key},
                                    },
                                )
                finally:
                    for socket in sockets:
                        socket.close(linger=0)
        except Exception as exc:
            self.error = exc
            log.exception("rules receiver failed")
            self.stop.set()
