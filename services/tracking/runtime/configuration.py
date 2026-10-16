"""Read the selected camera, model and tracker profile from original YAMLs."""

from pathlib import Path

from config.controller.loader import load_file
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models
from config.controller.parsers.tracker import parse as parse_tracker
from config.controller.parsers.tracker_algorithms import needs_frame


def load_settings(directory, camera_id):
    cameras = parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
    camera = next((c for c in cameras if c.id == camera_id and c.enabled), None)
    if camera is None:
        raise ValueError("selected camera is missing or disabled")
    models = parse_models(load_file(directory / "models.yaml", "models"))
    model = models.get(camera.model_id)
    if model is None or not model.use_tracker:
        raise ValueError("camera model is missing or tracking is disabled")
    reference = Path(model.tracker.replace("\\", "/"))
    if reference.parts and reference.parts[0] == "config":
        reference = Path(*reference.parts[1:])
    root = directory.resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        raise ValueError("tracker profile must be inside the configuration directory")
    tracker = parse_tracker(load_file(path, None))
    return camera, model, tracker, needs_frame(tracker.algorithm, tracker.params)
