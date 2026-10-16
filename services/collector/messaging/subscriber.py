"""Receive raw frames and inference results on private sockets with bounded transport queues."""

import threading

import zmq

from . import frames, detections


class Subscriber(threading.Thread):
    def __init__(self, camera_id, frame_endpoint, result_endpoint, inbox, worker, stop):
        super().__init__(name="collector-input", daemon=True)
        self.camera_id, self.frame_endpoint, self.result_endpoint = (
            camera_id,
            frame_endpoint,
            result_endpoint,
        )
        self.inbox, self.worker, self.stop = inbox, worker, stop
        self.error = None

    def run(self):
        context = zmq.Context()
        sockets = []
        disconnected = False
        try:
            poller = zmq.Poller()
            kinds = {}
            for name, endpoint, topic in (
                ("frames", self.frame_endpoint, "frame.v1"),
                ("results", self.result_endpoint, "detections.v1"),
            ):
                socket = context.socket(zmq.SUB)
                sockets.append(socket)
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.RCVHWM, min(self.inbox.limits[name][0], 4))
                socket.setsockopt(zmq.MAXMSGSIZE, self.inbox.limits[name][1])
                socket.setsockopt(zmq.SUBSCRIBE, f"{topic}/{self.camera_id}".encode())
                socket.connect(endpoint)
                poller.register(socket, zmq.POLLIN)
                kinds[socket] = name
            while not self.stop.is_set():
                if self.worker.snapshot()["state"] == "completed":
                    if not disconnected:
                        for socket in sockets:
                            poller.unregister(socket)
                            socket.close(linger=0)
                        sockets = []
                        disconnected = True
                    self.stop.wait(0.1)
                    continue
                for socket, _ in poller.poll(100):
                    parts = socket.recv_multipart()
                    try:
                        if kinds[socket] == "frames":
                            header, image = frames.decode(parts)
                            self.inbox.put_frame(header, image)
                        else:
                            self.inbox.put_result(
                                detections.decode(parts), len(parts[1])
                            )
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        OverflowError,
                        RecursionError,
                    ):
                        self.inbox.reject()
        except Exception as exc:
            self.error = exc
            self.stop.set()
        finally:
            for socket in sockets:
                socket.close(linger=0)
            context.term()
