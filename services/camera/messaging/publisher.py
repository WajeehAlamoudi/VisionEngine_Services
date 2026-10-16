"""Camera output adapter. Other services should also name their output adapter publisher.py."""

import zmq

from .frames import Frame, encode


class Publisher:
    """Thread-confined, best-effort fan-out; send success is not delivery acknowledgement."""

    def __init__(self, endpoint: str, *, high_water_mark: int = 2):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("publisher endpoint must use tcp:// or ipc://")
        if type(high_water_mark) is not int or high_water_mark < 1:
            raise ValueError("transport high-water mark must be a positive integer")
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.PUB)
        self._socket.setsockopt(zmq.SNDHWM, high_water_mark)
        self._socket.setsockopt(zmq.LINGER, 0)
        self.sent = 0
        try:
            self._socket.bind(endpoint)
        except BaseException:
            self.close()
            raise
        self.endpoint = self._socket.getsockopt_string(zmq.LAST_ENDPOINT)

    def publish(self, frame: Frame) -> None:
        self._socket.send_multipart(encode(frame))
        self.sent += 1

    def close(self) -> None:
        self._socket.close(linger=0)
        self._context.term()
