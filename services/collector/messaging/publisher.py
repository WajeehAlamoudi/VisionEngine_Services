"""Publish collection status; dataset files are written to persistent storage."""

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

    def publish(self, session_id, status):
        self.socket.send_multipart(
            [
                f"collector.status.v1/{session_id}".encode(),
                json.dumps(
                    {
                        "message_type": "collector.status.v1",
                        "session_id": session_id,
                        "collector": status,
                    },
                    allow_nan=False,
                ).encode(),
            ]
        )

    def close(self):
        self.socket.close(linger=0)
        self.context.term()
