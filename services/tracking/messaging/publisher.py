"""Publish tracked detection batches on a dedicated output endpoint."""

import zmq

from .detections import encode


class Publisher:
    def __init__(self, endpoint):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("output endpoint must use TCP or IPC")
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.SNDHWM, 32)
        try:
            self.socket.bind(endpoint)
        except BaseException:
            self.close()
            raise

    def publish(self, batch):
        self.socket.send_multipart(encode(batch))

    def close(self):
        self.socket.close(linger=0)
        self.context.term()
