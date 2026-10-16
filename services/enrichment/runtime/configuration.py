"""Read original camera/model settings without loading models or evaluating rules."""

from config.controller.loader import load_file
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models


def load_settings(directory, camera_id):
    cameras = parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
    camera = next((c for c in cameras if c.enabled and c.id == camera_id), None)
    if camera is None:
        raise ValueError("selected camera is missing or disabled")
    models = parse_models(load_file(directory / "models.yaml", "models"))
    model = models.get(camera.model_id)
    if model is None:
        raise ValueError("camera model is missing")
    if not set(camera.classes).issubset(model.classes):
        raise ValueError("camera classes do not match the selected model")
    return camera, model
