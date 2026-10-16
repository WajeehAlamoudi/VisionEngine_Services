"""Publish enriched event batches; each socket is owned by its calling thread."""

import zmq

from .events import encode


class Publisher:
    def __init__(self, endpoint):
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
