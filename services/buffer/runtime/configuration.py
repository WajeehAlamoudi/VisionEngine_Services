"""Read existing API/buffer settings without generating configuration copies."""

from config.controller.loader import load_file
from config.controller.parsers.api import parse as parse_api
from config.controller.parsers.device import parse as parse_device


def load_settings(directory):
    return (
        parse_api(load_file(directory / "api.yaml", "api")),
        parse_device(load_file(directory / "device.yaml", "device")),
    )
