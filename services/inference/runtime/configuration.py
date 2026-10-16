"""Load original YAML settings and explicit camera input endpoints."""

from dataclasses import dataclass
from pathlib import Path

from config.controller.loader import load_file
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models


@dataclass
class Input:
    camera: object
    endpoint: str


def load_settings(directory: Path, model_id: str, connections: list[str]):
    models = parse_models(load_file(directory / "models.yaml", "models"))
    if model_id not in models:
        raise ValueError("selected model is missing")
    model = models[model_id]
    cameras = {
        cam.id: cam
        for cam in parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
        if cam.enabled
    }
    inputs = {}
    for connection in connections:
        camera_id, separator, endpoint = connection.partition("=")
        if not separator or not endpoint.startswith(("tcp://", "ipc://")):
            raise ValueError(
                "each input must be CAMERA_ID=tcp://HOST:PORT or ipc://PATH"
            )
        if camera_id in inputs or camera_id not in cameras:
            raise ValueError("input camera is duplicated, missing, or disabled")
        camera = cameras[camera_id]
        if camera.model_id != model_id or not set(camera.classes).issubset(
            model.classes
        ):
            raise ValueError("input camera does not match this model or its classes")
        inputs[camera_id] = Input(camera, endpoint)
    if not inputs:
        raise ValueError("at least one camera input is required")
    return model, inputs
