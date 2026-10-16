"""Publish delivery status; actual row output is HTTP to the backend."""

import json

import zmq


class Publisher:
    def __init__(self, endpoint):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.SNDHWM, 4)
        try:
            self.socket.bind(endpoint)
        except Exception:
            self.close()
            raise

    def publish(self, device_id, status):
        self.socket.send_multipart(
            [
                f"notifier.status.v1/{device_id}".encode(),
                json.dumps(
                    {
                        "message_type": "notifier.status.v1",
                        "device_id": device_id,
                        "notifier": status,
                    },
                    allow_nan=False,
                ).encode(),
            ]
        )

    def close(self):
        self.socket.close(linger=0)
        self.context.term()
