from __future__ import annotations

from dataclasses import dataclass

from .strict import ID_PATTERN, TABLE_NAME_PATTERN, Reader

_FILE = "device.yaml"

_DEVICE_KEYS = (
    "id",
    "name",
    "location",
    "environment",
    "max_cameras",
    "log_level",
    "heartbeat",
    "health_file",
)
_HEARTBEAT_KEYS = ("enabled", "interval_seconds", "table")
_HEALTH_FILE_KEYS = ("enabled", "path", "interval_seconds")

ENVIRONMENTS = ("production", "development")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class HeartbeatConfig:
    enabled: bool
    interval_seconds: int
    table: str


@dataclass
class HealthFileConfig:
    enabled: bool
    path: str
    interval_seconds: int


@dataclass
class DeviceConfig:
    id: str
    name: str
    location: str
    # Selects the Compose project name: visionengine-<environment>-edge.
    # It is not part of the heartbeat row.
    environment: str  # production | development
    max_cameras: int
    log_level: str  # DEBUG | INFO | WARNING | ERROR
    heartbeat: HeartbeatConfig
    health_file: HealthFileConfig


def parse(raw: dict) -> DeviceConfig:
    """
    Parse and validate device.yaml.

    Every key is required, including those under a disabled heartbeat or
    health file - one rule ("always present") is more predictable than
    conditional requirements.
    """
    r = Reader(_FILE, raw)
    r.reject_unknown(*_DEVICE_KEYS)

    device_id = r.identifier(
        "id",
        ID_PATTERN,
        "letters, digits, hyphens, and underscores only (this becomes nodes.device_id)",
    )
    name = r.string("name")
    location = r.string("location")
    environment = r.enum("environment", ENVIRONMENTS)
    max_cameras = r.integer("max_cameras", minimum=1)
    log_level = r.enum("log_level", LOG_LEVELS)

    hb = r.section("heartbeat")
    hb.reject_unknown(*_HEARTBEAT_KEYS)
    heartbeat = HeartbeatConfig(
        enabled=hb.boolean("enabled"),
        interval_seconds=hb.integer("interval_seconds", minimum=1),
        table=hb.identifier(
            "table",
            TABLE_NAME_PATTERN,
            "letters, digits, and underscores only (a table name)",
        ),
    )

    hf = r.section("health_file")
    hf.reject_unknown(*_HEALTH_FILE_KEYS)
    health_file = HealthFileConfig(
        enabled=hf.boolean("enabled"),
        path=hf.string("path"),
        interval_seconds=hf.integer("interval_seconds", minimum=1),
    )

    r.raise_if_errors()

    return DeviceConfig(
        id=device_id,
        name=name,
        location=location,
        environment=environment,
        max_cameras=max_cameras,
        log_level=log_level,
        heartbeat=heartbeat,
        health_file=health_file,
    )
