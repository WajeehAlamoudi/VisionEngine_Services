from __future__ import annotations

from dataclasses import dataclass, field

from .strict import Reader

_FILE = "api.yaml"

# Backend-issued keys are long random strings. The exact format is the
# backend's business, but anything this short is a placeholder or a bad paste.
_MIN_KEY_LENGTH = 20


@dataclass
class IngestConfig:
    batch_size: int
    flush_interval_seconds: int


@dataclass
class BufferConfig:
    path: str
    max_size_mb: int
    retry_interval_seconds: int
    delete_after_hours: int


@dataclass
class RequestConfig:
    timeout_seconds: int
    max_consecutive_failures: int


@dataclass
class ApiConfig:
    branch_id: str  # which branch this device belongs to — used for alert routing
    key: str = field(repr=False)
    url: str  # trailing slash stripped at parse time
    ingest: IngestConfig
    buffer: BufferConfig
    request: RequestConfig


def parse(raw: dict) -> ApiConfig:
    """
    Parse and validate api.yaml.

    Every key is required — nothing is defaulted. Types, ranges, and formats
    are checked here so a bad value stops the device at startup rather than
    surfacing later as an auth failure or a request to a hostless URL.
    """
    r = Reader(_FILE, raw)
    r.reject_unknown("branch_id", "key", "url", "ingest", "buffer", "request")

    branch_id = r.uuid_string("branch_id")
    key = r.string("key", min_len=_MIN_KEY_LENGTH)
    url = r.url("url")

    ingest_r = r.section("ingest")
    ingest_r.reject_unknown("batch_size", "flush_interval_seconds")
    batch_size = ingest_r.integer("batch_size", minimum=1)
    flush_interval_seconds = ingest_r.integer("flush_interval_seconds", minimum=1)

    buffer_r = r.section("buffer")
    buffer_r.reject_unknown(
        "path", "max_size_mb", "retry_interval_seconds", "delete_after_hours"
    )
    buffer_path = buffer_r.string("path")
    max_size_mb = buffer_r.integer("max_size_mb", minimum=1)
    retry_interval_seconds = buffer_r.integer("retry_interval_seconds", minimum=1)
    # 0 is meaningful here: delete rows immediately after a successful push.
    delete_after_hours = buffer_r.integer("delete_after_hours", minimum=0)

    if buffer_path.startswith("/tmp"):
        buffer_r.warn(
            "path",
            "buffer lives under /tmp - unsent rows are lost on reboot",
        )

    request_r = r.section("request")
    request_r.reject_unknown("timeout_seconds", "max_consecutive_failures")
    timeout_seconds = request_r.integer("timeout_seconds", minimum=1)
    max_consecutive_failures = request_r.integer("max_consecutive_failures", minimum=1)

    r.raise_if_errors()

    return ApiConfig(
        branch_id=branch_id,
        key=key,
        url=url,
        ingest=IngestConfig(
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
        ),
        buffer=BufferConfig(
            path=buffer_path,
            max_size_mb=max_size_mb,
            retry_interval_seconds=retry_interval_seconds,
            delete_after_hours=delete_after_hours,
        ),
        request=RequestConfig(
            timeout_seconds=timeout_seconds,
            max_consecutive_failures=max_consecutive_failures,
        ),
    )
