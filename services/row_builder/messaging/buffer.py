"""Bounded, correlated buffer requests and replies over ZeroMQ REQ/REP."""

import json
import uuid

import zmq

MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def encode(document):
    payload = json.dumps(document, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("buffer message is too large")
    return payload


def decode(payload):
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("buffer message is too large")
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("expected a buffer mapping")
    json.dumps(document, allow_nan=False)
    return document


class BufferError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Client:
    def __init__(self, endpoint, timeout_ms=2000):
        self.endpoint, self.timeout_ms = endpoint, timeout_ms
        self.context = zmq.Context()
        self.socket = None

    def _reset(self):
        if self.socket is not None:
            self.socket.close(linger=0)
            self.socket = None

    def request(self, operation, **arguments):
        request_id = uuid.uuid4().hex
        payload = encode(
            {"version": 1, "request_id": request_id, "op": operation, **arguments}
        )
        if self.socket is None:
            self.socket = self.context.socket(zmq.REQ)
            self.socket.setsockopt(zmq.LINGER, 0)
            self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
            self.socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
            self.socket.connect(self.endpoint)
        try:
            self.socket.send(payload)
            if not self.socket.poll(self.timeout_ms):
                raise TimeoutError("buffer acknowledgement timed out")
            reply = decode(self.socket.recv())
        except zmq.Again as exc:
            self._reset()
            raise TimeoutError("buffer request timed out") from exc
        except Exception:
            # A REQ socket cannot send another request until its reply arrives.
            self._reset()
            raise
        if reply.get("request_id") != request_id or reply.get("version") != 1:
            raise ValueError("uncorrelated buffer reply")
        if not reply.get("ok"):
            raise BufferError(
                reply.get("code", "invalid"),
                reply.get("error", "buffer rejected request"),
            )
        return reply["result"]

    def close(self):
        self._reset()
        self.context.term()
