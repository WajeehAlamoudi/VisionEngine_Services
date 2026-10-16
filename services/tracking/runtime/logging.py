"""Service-local console and JSON logging; no dependencies on other services."""

from datetime import datetime, timezone
import json
import logging
import math
import os
import re
import sys


def _clean(value):
    if isinstance(value, str):
        value = re.sub(r"(?i)((?:rtsp|rtsps|http|https)://)[^/\s@]+@", r"\1***@", value)
        return re.sub(
            r"(?i)((?:password|passwd|token|api_key)=)[^&\s]+", r"\1***", value
        )
    if isinstance(value, dict):
        return {
            str(key): "***"
            if str(key).lower() in ("password", "token", "api_key", "secret")
            else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


class Formatter(logging.Formatter):
    LEVEL_COLORS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[31;1m",
    }
    STAGE_COLORS = {
        "input": "\033[36m",
        "processing": "\033[35m",
        "output": "\033[34m",
        "lifecycle": "\033[90m",
    }

    def __init__(
        self, service, instance, *, output="console", color=False, context=None
    ):
        super().__init__()
        self.service, self.instance = service, instance
        self.output, self.color = output, color and output == "console"
        self.context = context or {}

    def format(self, record):
        timestamp = (
            datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        fields = _clean({**self.context, **getattr(record, "fields", {})})
        stage = getattr(record, "stage", "lifecycle")
        message = _clean(record.getMessage())
        data = dict(
            timestamp=timestamp,
            level=record.levelname,
            service=self.service,
            instance=self.instance,
            stage=stage,
            event=getattr(record, "event", "log"),
            message=message,
            fields=fields,
        )
        if record.exc_info:
            data["exception"] = _clean(self.formatException(record.exc_info))
        if self.output == "json":
            return json.dumps(
                data, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
        level, label = (
            f"{record.levelname:<7}",
            f"{('PROCESS' if stage == 'processing' else stage.upper()):<9}",
        )
        if self.color:
            level = self.LEVEL_COLORS.get(record.levelname, "") + level + "\033[0m"
            label = self.STAGE_COLORS.get(stage, "") + label + "\033[0m"

        def display(value):
            if isinstance(value, float):
                return f"{value:.2f}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            return (
                str(value).replace("\n", "\\n").replace("\r", "\\r").replace("\033", "")
            )

        aliases = {
            "camera_id": "camera",
            "model_id": "model",
            "sequence": "seq",
            "stream_id": "stream",
            "captured_total": "captured",
            "received_total": "received",
            "processed_total": "processed",
            "sent_total": "sent",
            "detections_sent_total": "detections_sent",
            "annotated_sent_total": "frames_sent",
            "queued_frames": "queued",
            "queued_bytes": "queue_bytes",
            "dropped_total": "dropped",
            "reconnects_total": "reconnects",
            "last_inference_ms": "infer_ms",
            "payload_bytes": "bytes",
        }
        details = "  ".join(
            f"{aliases.get(key, key)}={display(value)}" for key, value in fields.items()
        )
        result = f"{timestamp[11:]}  {level}  {self.instance:<24}  {label}  {display(message)}"
        if details:
            result += " | " + details
        if "exception" in data:
            result += " | exception=" + display(data["exception"])
        return result


def add_arguments(parser):
    parser.add_argument(
        "--log-format",
        choices=("console", "json"),
        default=os.environ.get("VISIONENGINE_LOG_FORMAT", "console"),
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=os.environ.get("VISIONENGINE_LOG_LEVEL", "INFO"),
    )
    parser.add_argument(
        "--log-color",
        choices=("auto", "always", "never"),
        default=os.environ.get("VISIONENGINE_LOG_COLOR", "auto"),
    )


def configure(args, service, instance, **context):
    stream = sys.stderr
    color = args.log_color == "always" or (
        args.log_color == "auto" and stream.isatty() and "NO_COLOR" not in os.environ
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        Formatter(
            service, instance, output=args.log_format, color=color, context=context
        )
    )
    logging.basicConfig(level=args.log_level, handlers=[handler], force=True)


def print_message(header, service, *, color="auto"):
    fields = {
        key: header[key]
        for key in (
            "camera_id",
            "stream_id",
            "sequence",
            "model_id",
            "model_version",
            "width",
            "height",
            "payload_bytes",
            "inference_ms",
        )
        if key in header
    }
    if "detections" in header:
        fields["detections"] = len(header["detections"])
        fields["track_ids"] = [
            d["track_id"] for d in header["detections"] if d.get("track_id")
        ]
    if "tracking" in header:
        fields.update(header["tracking"])
    if "annotations" in header:
        fields.update(header["annotations"])
    record = logging.LogRecord(
        service, logging.INFO, "", 0, header["message_type"] + " received", (), None
    )
    record.stage, record.event, record.fields = "output", "message_received", fields
    enabled = color == "always" or (
        color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    print(
        Formatter(service, service + "-receiver", color=enabled).format(record),
        flush=True,
    )
