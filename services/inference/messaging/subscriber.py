"""Own all SUB sockets on one receiver thread; one socket per configured camera."""

import logging
import threading
import time

import zmq

from services.inference.messaging.frames import MAX_FRAME_BYTES, decode

log = logging.getLogger(__name__)


class Subscriber(threading.Thread):
    def __init__(self, inputs, inbox, stop):
        super().__init__(name="inference-inputs", daemon=True)
        self.inputs, self.inbox, self.stop = inputs, inbox, stop
        self.error = None

    def run(self):
        context = None
        sockets = {}
        poller = zmq.Poller()
        try:
            context = zmq.Context()
            for camera_id, source in self.inputs.items():
                socket = context.socket(zmq.SUB)
                sockets[socket] = camera_id
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.RCVHWM, self.inbox.max_items)
                socket.setsockopt(zmq.MAXMSGSIZE, MAX_FRAME_BYTES)
                socket.setsockopt(zmq.SUBSCRIBE, f"frame.v1/{camera_id}".encode())
                socket.connect(source.endpoint)
                poller.register(socket, zmq.POLLIN)
            while not self.stop.is_set():
                for socket, _ in poller.poll(100):
                    try:
                        header, image = decode(socket.recv_multipart())
                        if header["camera_id"] == sockets[socket]:
                            self.inbox.put(header, image, time.monotonic())
                            log.debug(
                                "frame received",
                                extra={
                                    "stage": "input",
                                    "event": "frame_received",
                                    "fields": {
                                        "camera_id": header["camera_id"],
                                        "stream_id": header["stream_id"],
                                        "sequence": header["sequence"],
                                    },
                                },
                            )
                    except (ValueError, UnicodeError):
                        log.warning(
                            "discarded an invalid frame for camera %s", sockets[socket]
                        )
        except Exception as exc:
            self.error = type(exc).__name__
            log.error("frame receiver failed (%s)", self.error)
            self.stop.set()
        finally:
            for socket in sockets:
                socket.close(linger=0)
            if context:
                context.term()
