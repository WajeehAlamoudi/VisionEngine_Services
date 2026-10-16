from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .strict import ALL, ID_PATTERN, ZONE_NAME_PATTERN, Reader, describe

_FILE = "cameras.yaml"

_CAMERA_KEYS = (
    "id",
    "name",
    "source",
    "enabled",
    "fps_target",
    "model_id",
    "classes",
    "raw_table",
    "routing",
    "zones",
)
_ROUTING_KEYS = ("classes", "raw_table")
_ZONE_KEYS = ("name", "polygon")

# Above this, a camera is almost certainly misconfigured rather than ambitious.
_FPS_WARN_ABOVE = 30


@dataclass
class Zone:
    name: str
    polygon: list[list[int]]  # [[x, y], ...] pixel coordinates


@dataclass
class RoutingEntry:
    classes: list[str]  # [] = all — resolved from "*" at parse time
    raw_table: str


@dataclass
class CameraConfig:
    id: str
    name: str
    source: str | int  # int = USB index, str = RTSP URL or file path
    enabled: bool
    fps_target: int
    model_id: str
    classes: list[
        str
    ]  # [] = all — resolved from "*", resolved when the model parser is added
    raw_table: str | None  # single-table mode
    routing: list[
        RoutingEntry
    ]  # class-routing mode (mutually exclusive with raw_table)
    zones: list[Zone]  # [] = full frame — resolved from "*"


def _parse_zones(r: Reader, path: str) -> list[Zone]:
    """Zones for one camera. "*" means full frame, stored internally as []."""
    raw = r.list_or_all("zones")
    if raw == ALL or not raw:
        return []

    zones: list[Zone] = []
    seen: set[str] = set()

    for i, item in enumerate(raw):
        where = f"{path}.zones[{i}]"
        zr = r.child(where, item)
        zr.reject_unknown(*_ZONE_KEYS)

        name = zr.identifier(
            "name",
            ZONE_NAME_PATTERN,
            "letters, digits, and underscores only (rules reference this name)",
        )
        polygon = zr.polygon("polygon")

        if name:
            if name in seen:
                r.error(f"{where}.name", f"duplicate zone name '{name}' on this camera")
            seen.add(name)

        zones.append(Zone(name=name, polygon=polygon))

    return zones


def _parse_routing(r: Reader, path: str) -> list[RoutingEntry]:
    """Optional class->table routing. Absent means single-table mode."""
    if "routing" not in r.raw:
        return []

    raw = r.list_or_all("routing")
    if raw == ALL:
        r.error(f"{path}.routing", 'expected a list of routing entries, got "*"')
        return []
    if not raw:
        return []

    entries: list[RoutingEntry] = []
    for i, item in enumerate(raw):
        where = f"{path}.routing[{i}]"
        er = r.child(where, item)
        er.reject_unknown(*_ROUTING_KEYS)

        classes = er.string_list_or_all("classes")
        entries.append(
            RoutingEntry(
                classes=[] if classes == ALL else list(classes),
                raw_table=er.string("raw_table"),
            )
        )
    return entries


def _parse_one(r: Reader, path: str) -> CameraConfig:
    r.reject_unknown(*_CAMERA_KEYS)

    cam_id = r.identifier(
        "id", ID_PATTERN, "letters, digits, hyphens, and underscores only"
    )
    name = r.string("name")
    source = r.camera_source("source")
    enabled = r.boolean("enabled")
    fps_target = r.integer("fps_target", minimum=1)
    model_id = r.string("model_id")
    classes = r.string_list_or_all("classes")

    if fps_target > _FPS_WARN_ABOVE:
        r.warn("fps_target", f"{fps_target} fps is unusually high for one camera")

    has_raw_table = "raw_table" in r.raw
    has_routing = "routing" in r.raw

    if has_raw_table and has_routing:
        r.error(path, "use raw_table OR routing, not both")
    elif not has_raw_table and not has_routing:
        r.error(
            path,
            "one of raw_table or routing is required - detections need somewhere to go",
        )

    raw_table = r.string("raw_table") if has_raw_table else None
    routing = _parse_routing(r, path) if has_routing else []

    return CameraConfig(
        id=cam_id,
        name=name,
        source=source,
        enabled=enabled,
        fps_target=fps_target,
        model_id=model_id,
        classes=[] if classes == ALL else list(classes),
        raw_table=raw_table,
        routing=routing,
        zones=_parse_zones(r, path),
    )


def parse_all(raw: Any) -> list[CameraConfig]:
    """
    Parse and validate every camera in cameras.yaml.

    Errors from all cameras are collected and raised together, so one restart
    surfaces every problem in the file rather than the first one only.

    Cross-file checks (does model_id exist, are the classes known to that
    model) belong to a later cross-file validation pass.
    """
    root = Reader(_FILE, {})

    if not isinstance(raw, list):
        root.error("cameras", f"expected a list of cameras, got {describe(raw)}")
        root.raise_if_errors()
    if not raw:
        root.error("cameras", "at least one camera must be defined")
        root.raise_if_errors()

    cameras: list[CameraConfig] = []
    seen: set[str] = set()

    for i, item in enumerate(raw):
        path = f"cameras[{i}]"
        cam = _parse_one(root.child(path, item), path)

        if cam.id:
            if cam.id in seen:
                # ModelRegistry keys runners by camera id, so a duplicate
                # silently replaces the first camera instead of failing.
                root.error(f"{path}.id", f"duplicate camera id '{cam.id}'")
            seen.add(cam.id)

        cameras.append(cam)

    root.raise_if_errors()
    return cameras
