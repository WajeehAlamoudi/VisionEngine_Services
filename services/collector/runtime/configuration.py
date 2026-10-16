"""Read original collection/camera/model configuration; no parent-project dependencies."""

from config.controller.loader import load_file
from config.controller.parsers.collection import parse as parse_collection
from config.controller.parsers.camera import parse_all as parse_cameras
from config.controller.parsers.model import parse_all as parse_models


def load_settings(directory, session_id):
    collection = parse_collection(
        load_file(directory / "collection.yaml", "collection")
    )
    session = next(
        (s for s in collection.sessions if s.id == session_id and s.enabled), None
    )
    if session is None:
        raise ValueError("unknown or disabled collection session")
    cameras = parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
    camera = next((c for c in cameras if c.id == session.camera and c.enabled), None)
    if camera is None:
        raise ValueError("collection camera is missing or disabled")
    models = parse_models(load_file(directory / "models.yaml", "models"))
    model = models.get(camera.model_id)
    if model is None:
        raise ValueError("collection camera model is missing")
    if set(session.filters.classes) - set(camera.classes or model.classes):
        raise ValueError("collection classes are not active on the camera")
    return collection, session, camera, model
