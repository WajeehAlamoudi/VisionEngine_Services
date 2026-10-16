"""Parse the version-1 graph template without creating runtime nodes."""

from dataclasses import dataclass
from typing import Any
import math
import re

from .strict import ConfigError

_ID = re.compile(r"[A-Za-z0-9_/-]+")
_CONTEXTS = {
    "enabled_cameras",
    "used_models",
    "tracked_cameras",
    "untracked_cameras",
    "enabled_collection_sessions",
}
_CONDITIONS = {
    "always",
    "notifications_enabled",
    "tracking_enabled",
    "tracking_disabled",
    "tracking_requires_frame",
}
_PLACEHOLDERS = {
    "camera.id",
    "camera.model_id",
    "model.id",
    "session.id",
    "session.camera",
    "device.id",
}


@dataclass(frozen=True)
class GraphConfig:
    version: int
    services: dict
    profiles: dict
    nodes: list[dict]
    edges: list[dict]


def _error(path, message):
    raise ConfigError("graph.yaml", [f"{path}: {message}"])


def _mapping(value, path):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        _error(path, "expected a mapping with string keys")
    return value


def _keys(value, allowed, path):
    unknown = set(value) - set(allowed)
    if unknown:
        _error(path, f"unknown fields: {', '.join(sorted(unknown))}")


def _string(value, path):
    if not isinstance(value, str) or not value.strip():
        _error(path, "expected a non-empty string")
    return value


def _template(value, path):
    _string(value, path)
    fields = re.findall(r"\{([^{}]*)\}", value)
    if any(field not in _PLACEHOLDERS for field in fields):
        _error(path, "unsupported template placeholder")
    remainder = re.sub(r"\{[^{}]*\}", "", value)
    if "{" in remainder or "}" in remainder:
        _error(path, "unbalanced template braces")


def parse(raw: Any) -> GraphConfig:
    raw = _mapping(raw, "graph")
    _keys(raw, {"version", "services", "profiles", "nodes", "edges"}, "graph")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        _error("version", "expected graph schema version 1")
    services = _mapping(raw.get("services"), "services")
    if not services:
        _error("services", "at least one service contract is required")
    for name, service in services.items():
        if not _ID.fullmatch(name):
            _error("services", "invalid service name")
        service = _mapping(service, f"services.{name}")
        _keys(service, {"inputs", "outputs", "required_inputs"}, f"services.{name}")
        for direction in ("inputs", "outputs"):
            ports = _mapping(service.get(direction, {}), f"services.{name}.{direction}")
            for port, kind in ports.items():
                if not re.fullmatch(r"[A-Za-z0-9_]+", port):
                    _error(f"services.{name}", "invalid port name")
                _string(kind, f"services.{name}.{direction}.{port}")
        required = service.get("required_inputs", list(service.get("inputs", {})))
        if not isinstance(required, list) or any(
            not isinstance(p, str) or p not in service.get("inputs", {})
            for p in required
        ):
            _error(
                f"services.{name}.required_inputs", "expected declared input port names"
            )
    profiles = _mapping(raw.get("profiles"), "profiles")
    for name, profile in profiles.items():
        profile = _mapping(profile, f"profiles.{name}")
        _keys(
            profile,
            {"max_items", "max_bytes", "max_age_ms", "overflow", "ordering"},
            f"profiles.{name}",
        )
        for field in ("max_items", "max_bytes"):
            if type(profile.get(field)) is not int or profile[field] < 1:
                _error(f"profiles.{name}.{field}", "expected a positive integer")
        age = profile.get("max_age_ms")
        if age is not None and (
            type(age) not in (int, float) or not math.isfinite(age) or age <= 0
        ):
            _error(
                f"profiles.{name}.max_age_ms",
                "expected a positive finite number or null",
            )
        if profile.get("overflow") not in ("drop_oldest", "drop_newest", "block"):
            _error(f"profiles.{name}.overflow", "unsupported overflow policy")
        if profile.get("ordering", "none") not in ("none", "camera"):
            _error(f"profiles.{name}.ordering", "expected none or camera")
    for kind in ("nodes", "edges"):
        items = raw.get(kind)
        if not isinstance(items, list):
            _error(kind, "expected a list")
        for index, item in enumerate(items):
            path = f"{kind}[{index}]"
            item = _mapping(item, path)
            common = {"id", "for_each", "when", "enabled"}
            allowed = common | (
                {"service", "config_refs"}
                if kind == "nodes"
                else {"from", "to", "profile", "select"}
            )
            _keys(item, allowed, path)
            _template(item.get("id"), path + ".id")
            if "for_each" in item and (
                not isinstance(item["for_each"], str)
                or item["for_each"] not in _CONTEXTS
            ):
                _error(path + ".for_each", "unsupported expansion collection")
            if (
                not isinstance(item.get("when", "always"), str)
                or item.get("when", "always") not in _CONDITIONS
            ):
                _error(path + ".when", "unsupported condition")
            if type(item.get("enabled", True)) is not bool:
                _error(path + ".enabled", "expected true or false")
            if kind == "nodes":
                if (
                    not isinstance(item.get("service"), str)
                    or item["service"] not in services
                ):
                    _error(path + ".service", "unknown service contract")
                for alias, ref in _mapping(
                    item.get("config_refs", {}), path + ".config_refs"
                ).items():
                    _template(ref, path + ".config_refs." + alias)
            else:
                for endpoint in ("from", "to"):
                    _template(item.get(endpoint), path + "." + endpoint)
                if (
                    not isinstance(item.get("profile"), str)
                    or item["profile"] not in profiles
                ):
                    _error(path + ".profile", "unknown connection profile")
                for key, value in _mapping(
                    item.get("select", {}), path + ".select"
                ).items():
                    if key not in ("camera_id",):
                        _error(
                            path + ".select", "only camera_id selection is supported"
                        )
                    _template(value, path + ".select." + key)
    return GraphConfig(1, services, profiles, raw["nodes"], raw["edges"])
