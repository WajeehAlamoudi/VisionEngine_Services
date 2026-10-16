"""Two live outputs: accepted event batches and alert batches."""

import zmq

from .events import encode as encode_events
from .alerts import encode as encode_alerts


class Publisher:
    def __init__(self, accepted_endpoint, alerts_endpoint):
        self.context = zmq.Context()
        self.sockets = []
        try:
            for endpoint in (accepted_endpoint, alerts_endpoint):
                socket = self.context.socket(zmq.PUB)
                self.sockets.append(socket)
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.SNDHWM, 32)
                socket.bind(endpoint)
        except BaseException:
            self.close()
            raise

    def publish(self, accepted, alerts):
        # Validate both before either send; PUB/SUB is not transactional delivery.
        event_parts = encode_events(accepted, ruled=True)
        alert_parts = encode_alerts(alerts)
        self.sockets[0].send_multipart(event_parts)
        self.sockets[1].send_multipart(alert_parts)

    def close(self):
        for socket in self.sockets:
            socket.close(linger=0)
        self.context.term()
