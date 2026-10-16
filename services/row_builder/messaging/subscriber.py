"""Receive accepted events and alerts independently; never join them by sequence."""

import logging
import threading

import zmq

from . import events, alerts, health

log = logging.getLogger(__name__)


class Subscriber(threading.Thread):
    def __init__(self, inputs, builder, inbox, stop):
        super().__init__(name="row-builder-input", daemon=True)
        self.inputs, self.builder, self.inbox, self.stop = inputs, builder, inbox, stop
        self.error = None

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
                        socket.setsockopt(zmq.MAXMSGSIZE, alerts.MAX_BATCH_BYTES)
                        socket.setsockopt(zmq.SUBSCRIBE, (source.kind + "/").encode())
                        socket.connect(source.endpoint)
                        poller.register(socket, zmq.POLLIN)
                    while not self.stop.is_set():
                        for socket, _ in poller.poll(100):
                            key = sockets[socket]
                            parts = socket.recv_multipart()
                            try:
                                source = self.inputs[key]
                                batch = (
                                    events.decode(parts, ruled=True)
                                    if source.kind == "event.v1"
                                    else health.decode(parts)
                                    if source.kind == "health.v1"
                                    else alerts.decode(parts)
                                )
                                self.builder.validate_input(batch)
                                self.inbox.put(key, batch, len(parts[1]))
                            except (ValueError, TypeError, KeyError, UnicodeError):
                                self.inbox.reject(key)
                                log.warning(
                                    "invalid row input discarded",
                                    extra={
                                        "stage": "input",
                                        "event": "invalid_input",
                                        "fields": {"input": key},
                                    },
                                )
                finally:
                    for socket in sockets:
                        socket.close(linger=0)
        except Exception as exc:
            self.error = exc
            log.exception("row input receiver failed")
            self.stop.set()
