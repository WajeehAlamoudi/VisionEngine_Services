"""Load one camera's settings using the existing configuration parser."""

from pathlib import Path

from config.controller.loader import load_file
from config.controller.parsers.camera import CameraConfig, parse_all


def load_camera(
    directory: Path, camera_id: str, pipeline: Path | None = None
) -> CameraConfig:
    cameras = parse_all(load_file(directory / "cameras.yaml", "cameras"))
    camera = next((item for item in cameras if item.id == camera_id), None)
    if camera is None or not camera.enabled:
        raise ValueError("selected camera is missing or disabled")
    if pipeline is not None:
        plan = load_file(pipeline, None)
        if plan.get("version") != 1 or not isinstance(plan.get("nodes"), list):
            raise ValueError("expected a version 1 generated pipeline")
        matches = [
            node
            for node in plan["nodes"]
            if isinstance(node, dict) and node.get("id") == f"camera/{camera_id}"
        ]
        if len(matches) != 1 or matches[0].get("service") != "camera":
            raise ValueError(
                "selected camera node is missing or invalid in the pipeline"
            )
        references = matches[0].get("config_refs")
        if (
            not isinstance(references, dict)
            or references.get("camera") != f"cameras/{camera_id}"
        ):
            raise ValueError(
                "pipeline camera reference does not match the selected camera"
            )
    # File locations are relative to the independent project's working directory,
    # matching the existing cameras.yaml convention. Never resolve against Edge.
    if isinstance(camera.source, str) and "://" not in camera.source:
        camera.source = str(Path(camera.source).resolve())
        if not Path(camera.source).is_file():
            raise ValueError("selected camera video file does not exist")
    return camera
