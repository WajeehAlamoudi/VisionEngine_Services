"""Load original camera/model/rule files and explicit camera input endpoints."""

from dataclasses import dataclass

from config.controller.loader import load_file
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models
from config.controller.parsers.rule import parse_all as parse_rules
from config.controller.validation import validate_references


@dataclass
class Input:
    camera: object
    model: object
    endpoint: str


def load_settings(directory, connections):
    cameras = parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
    models = parse_models(load_file(directory / "models.yaml", "models"))
    rules = parse_rules(load_file(directory / "rules.yaml", "rules"))
    validate_references({"cameras": cameras, "models": models, "rules": rules})
    by_id = {camera.id: camera for camera in cameras if camera.enabled}
    inputs = {}
    for connection in connections:
        camera_id, separator, endpoint = connection.partition("=")
        if not separator or not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError(
                "each input must be CAMERA_ID=tcp://HOST:PORT or ipc://PATH"
            )
        if camera_id not in by_id or camera_id in inputs:
            raise ValueError("input camera is missing, disabled or duplicated")
        camera = by_id[camera_id]
        inputs[camera_id] = Input(camera, models[camera.model_id], endpoint)
    if not inputs:
        raise ValueError("at least one enrichment input is required")
    return rules, inputs
