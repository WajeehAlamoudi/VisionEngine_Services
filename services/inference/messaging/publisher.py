"""Publish detections and optionally annotated frames, each on its own endpoint."""

import zmq
import logging

from services.inference.messaging.detections import encode as encode_detections
from services.inference.messaging.frames import encode as encode_frame

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, endpoint, annotated_endpoint=None, high_water_mark=2):
        self.context = zmq.Context()
        self.sockets = []
        try:
            self.detections = self._bind(endpoint, high_water_mark)
            self.annotated = (
                self._bind(annotated_endpoint, high_water_mark)
                if annotated_endpoint
                else None
            )
        except BaseException:
            self.close()
            raise

    def _bind(self, endpoint, high_water_mark):
        if not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError("output endpoint must use TCP or IPC")
        socket = self.context.socket(zmq.PUB)
        self.sockets.append(socket)
        socket.setsockopt(zmq.SNDHWM, high_water_mark)
        socket.setsockopt(zmq.LINGER, 0)
        socket.bind(endpoint)
        return socket

    def publish(self, batch, annotated_frame=None):
        self.detections.send_multipart(encode_detections(batch))
        if annotated_frame is not None and self.annotated is not None:
            self.annotated.send_multipart(encode_frame(annotated_frame))
        log.debug(
            "results sent",
            extra={
                "stage": "output",
                "event": "results_sent",
                "fields": {
                    "camera_id": batch["camera_id"],
                    "stream_id": batch["stream_id"],
                    "sequence": batch["sequence"],
                    "detections": len(batch["detections"]),
                    "annotated": annotated_frame is not None
                    and self.annotated is not None,
                },
            },
        )

    def close(self):
        for socket in self.sockets:
            socket.close(linger=0)
        self.context.term()
