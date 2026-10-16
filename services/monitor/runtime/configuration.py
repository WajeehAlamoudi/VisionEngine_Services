"""Load device/camera identity and explicit file-to-stage assignments."""

from pathlib import Path

from config.controller.loader import load_file
from config.controller.parsers.device import parse as parse_device
from config.controller.parsers.camera import parse_all as parse_cameras


def load_settings(directory, watches, stage_entries, counter_entries):
    device = parse_device(load_file(directory / "device.yaml", "device"))
    cameras = {
        c.id: c
        for c in parse_cameras(load_file(directory / "cameras.yaml", "cameras"))
        if c.enabled
    }
    sources, stages, counters = {}, {key: [] for key in cameras}, {}
    for value in watches:
        name, separator, path = value.partition("=")
        if not name or not separator or not path or name in sources:
            raise ValueError("watch must be a unique SERVICE=PATH")
        sources[name] = Path(path)
    for value in stage_entries:
        camera, separator, name = value.partition("=")
        if (
            not separator
            or camera not in cameras
            or name not in sources
            or name in stages[camera]
        ):
            raise ValueError(
                "camera-stage must name a configured camera and unique watched service"
            )
        stages[camera].append(name)
    for value in counter_entries:
        camera, separator, name = value.partition("=")
        if (
            not separator
            or camera not in cameras
            or name not in sources
            or camera in counters
        ):
            raise ValueError(
                "camera-counter must name a configured camera and one watched service"
            )
        counters[camera] = name
    return device, cameras, sources, stages, counters
