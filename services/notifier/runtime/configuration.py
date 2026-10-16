"""Read installation configuration; resolve SMTP secrets only at service startup."""

import os
import logging
from config.controller.loader import load_file
from config.controller.parsers.api import parse as parse_api
from config.controller.parsers.device import parse as parse_device
from config.controller.parsers.notifications import parse as parse_notifications


def load_settings(directory):
    settings = parse_notifications(
        load_file(directory / "notifications.yaml", "notifications")
    )
    passwords = {}
    for channel in settings.emails:
        if channel.enabled and channel.smtp.password_env:
            value = os.environ.get(channel.smtp.password_env)
            if not value:
                logging.getLogger("notifier").error(
                    "SMTP password environment variable is missing",
                    extra={
                        "fields": {
                            "channel": channel.name,
                            "variable": channel.smtp.password_env,
                        }
                    },
                )
                raise ValueError(
                    "missing SMTP password environment variable for channel "
                    + channel.name
                )
            passwords[channel.name] = value
    return (
        settings,
        parse_api(load_file(directory / "api.yaml", "api")),
        parse_device(load_file(directory / "device.yaml", "device")),
        passwords,
    )
