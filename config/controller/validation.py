"""Relationships between parsed configuration files; no host/device checks."""

import logging
from typing import Any, Mapping

from .parsers.strict import ConfigError

log = logging.getLogger(__name__)


def validate_references(config: Mapping[str, Any]) -> None:
    problems = []
    cameras = config.get("cameras")
    models = config.get("models")
    if cameras is not None and "device" in config:
        if sum(camera.enabled for camera in cameras) > config["device"].max_cameras:
            problems.append("device.max_cameras: fewer than the enabled camera count")

    if cameras is not None and models is not None:
        for camera in cameras:
            where = f"cameras[{camera.id}]"
            model = models.get(camera.model_id)
            if model is None:
                problems.append(f"{where}.model_id: unknown model '{camera.model_id}'")
                continue
            if not camera.classes:
                camera.classes = list(model.classes)
            unknown = set(camera.classes) - set(model.classes)
            if unknown:
                problems.append(
                    f"{where}.classes: not declared by model: {sorted(unknown)}"
                )
            covered = set()
            for route in camera.routing:
                bad = set(route.classes) - set(model.classes)
                if bad:
                    problems.append(
                        f"{where}.routing: unknown model classes {sorted(bad)}"
                    )
                covered.update(route.classes or camera.classes)
            if camera.routing and set(camera.classes) - covered:
                log.warning("%s: some camera classes have no storage route", where)

    rules = config.get("rules")
    if rules is not None and cameras is not None:
        by_camera = {camera.id: camera for camera in cameras}
        all_zones = {zone.name for camera in cameras for zone in camera.zones}
        active_cameras = [camera for camera in cameras if camera.enabled]
        active_zones = {zone.name for camera in active_cameras for zone in camera.zones}
        for rule in rules:
            if not rule.enabled:
                continue
            where = f"rules[{rule.name}]"
            for camera_id in rule.cameras:
                if camera_id not in by_camera:
                    problems.append(f"{where}.cameras: unknown camera '{camera_id}'")
                elif not by_camera[camera_id].enabled:
                    log.warning("%s: camera '%s' is disabled", where, camera_id)
            for zone in rule.zones:
                if zone not in all_zones:
                    problems.append(f"{where}.zones: unknown zone '{zone}'")
                elif zone not in active_zones:
                    log.warning(
                        "%s: zone '%s' exists only on disabled cameras", where, zone
                    )
            if models is not None and rule.class_name != "*":
                known = {name for model in models.values() for name in model.classes}
                active = {name for camera in active_cameras for name in camera.classes}
                if rule.class_name not in known:
                    problems.append(
                        f"{where}.class: unknown model class '{rule.class_name}'"
                    )
                elif rule.class_name not in active:
                    log.warning("%s: class is not active on enabled cameras", where)
            # A named camera must actually be able to satisfy the named zones.
            selected = (
                [by_camera[c] for c in rule.cameras if c in by_camera]
                if rule.cameras
                else cameras
            )
            for zone in rule.zones:
                if zone in all_zones and not any(
                    zone in {z.name for z in c.zones} for c in selected
                ):
                    problems.append(
                        f"{where}.zones: zone '{zone}' is absent from selected cameras"
                    )

    if rules is not None and "notifications" in config:
        by_rule = {rule.name: rule for rule in rules}
        for webhook in [
            *config["notifications"].webhooks,
            *config["notifications"].emails,
        ]:
            if not webhook.enabled:
                continue
            for name in webhook.rules:
                if name not in by_rule:
                    problems.append(
                        f"notifications[{webhook.name}].rules: unknown rule '{name}'"
                    )
                elif not by_rule[name].enabled:
                    log.warning(
                        "notification channel '%s': rule '%s' is disabled",
                        webhook.name,
                        name,
                    )

    if cameras is not None and "collection" in config:
        by_camera = {camera.id: camera for camera in cameras}
        for session in config["collection"].sessions:
            where = f"collection[{session.id}]"
            camera = by_camera.get(session.camera)
            if camera is None:
                problems.append(f"{where}.camera: unknown camera '{session.camera}'")
                continue
            if session.enabled and not camera.enabled:
                log.warning("%s: collection camera is disabled", where)
            if models is not None:
                unknown = set(session.filters.classes) - set(camera.classes)
                if unknown:
                    problems.append(
                        f"{where}.filters.classes: not active on camera: {sorted(unknown)}"
                    )
    if problems:
        raise ConfigError("references", problems)


def validate_config(config: Mapping[str, Any]) -> None:
    """Validate relationships after each file parser has validated its fields."""
    validate_references(config)
