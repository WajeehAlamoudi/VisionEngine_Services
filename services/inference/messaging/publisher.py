"""Thread-confined publishers for independent detection and preview outputs."""

import logging
import zmq

from services.inference.messaging.detections import encode as encode_detections
from services.inference.messaging.frames import encode as encode_frame

log = logging.getLogger(__name__)


class _SocketPublisher:
    """Create, use and close this instance on the same owning thread."""

    def __init__(self, endpoint, high_water_mark=2):
        self.context = zmq.Context()
        self.socket = None
        try:
            self.socket = self.context.socket(zmq.PUB)
            if not endpoint.startswith(("tcp://", "ipc://")):
                raise ValueError("output endpoint must use TCP or IPC")
            self.socket.setsockopt(zmq.SNDHWM, high_water_mark)
            self.socket.setsockopt(zmq.LINGER, 0)
            self.socket.bind(endpoint)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.socket is not None:
            self.socket.close(linger=0)
        self.context.term()


class Publisher(_SocketPublisher):
    def publish(self, batch):
        self.socket.send_multipart(encode_detections(batch))
        log.debug(
            "detections sent",
            extra={
                "stage": "output",
                "event": "results_sent",
                "fields": {
                    "camera_id": batch["camera_id"],
                    "stream_id": batch["stream_id"],
                    "sequence": batch["sequence"],
                    "detections": len(batch["detections"]),
                },
            },
        )


class AnnotatedPublisher(_SocketPublisher):
    def publish(self, frame):
        self.socket.send_multipart(encode_frame(frame))
