"""Load original camera/model/rule settings and explicit rules endpoints."""

from dataclasses import dataclass

from config.controller.loader import load_file
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models
from config.controller.parsers.rule import parse_all as parse_rules
from config.controller.parsers.device import parse as parse_device
from config.controller.validation import validate_references


@dataclass
class Input:
    kind: str
    endpoint: str


def load_device(directory):
    return parse_device(load_file(directory / "device.yaml", "device"))


def load_settings(directory, events_inputs, alerts_inputs, monitor_inputs=None):
    cameras = parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
    models = parse_models(load_file(directory / "models.yaml", "models"))
    rules = parse_rules(load_file(directory / "rules.yaml", "rules"))
    validate_references({"cameras": cameras, "models": models, "rules": rules})
    inputs, seen = {}, set()
    if not events_inputs and not monitor_inputs:
        raise ValueError("at least one accepted-event or monitor input is required")
    for kind, endpoints in (
        ("event.v1", events_inputs),
        ("alert.v1", alerts_inputs),
        ("health.v1", monitor_inputs),
    ):
        for index, endpoint in enumerate(endpoints or []):
            if not endpoint.startswith(("tcp://", "ipc://")):
                raise ValueError("input endpoints must use TCP or IPC")
            if (kind, endpoint) in seen:
                raise ValueError("duplicate input endpoint")
            seen.add((kind, endpoint))
            inputs[f"{kind}:{index}"] = Input(kind, endpoint)
    return {c.id: c for c in cameras if c.enabled}, models, rules, inputs
