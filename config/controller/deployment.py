"""Compile camera, inference and tracking nodes into one local Compose deployment."""

from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re

import yaml

from .parsers.strict import ConfigError
from .writer import dump_yaml
from .parsers.tracker_algorithms import BOXMOT_ALGORITHMS, needs_frame


@dataclass
class Deployment:
    compose: dict


def _service_name(node):
    """Name Compose instances consistently without changing graph node IDs."""
    kind = node["service"].replace("_", "-")
    node_id = re.sub(r"[/_]+", "-", node["id"]).lower().strip("-")
    if not node_id:
        raise ConfigError("deployment", ["node cannot form a Compose service name"])
    suffix = (
        node_id[len(kind) :]
        if node_id == kind or node_id.startswith(kind + "-")
        else "-" + node_id
    )
    return f"visionengine-{kind}-service{suffix}"


def _relative(path: Path, directory: Path) -> str:
    """Compose resolves paths relative to the generated Compose file."""
    try:
        return Path(os.path.relpath(path, directory)).as_posix()
    except ValueError:  # Different Windows drives cannot form a relative path.
        return path.as_posix()


def _logging_environment(config):
    level = getattr(config["device"], "log_level", "INFO")
    return {
        "VISIONENGINE_LOG_FORMAT": "${VISIONENGINE_LOG_FORMAT:-console}",
        "VISIONENGINE_LOG_LEVEL": "${VISIONENGINE_LOG_LEVEL:-" + level + "}",
        "VISIONENGINE_LOG_COLOR": "${VISIONENGINE_LOG_COLOR:-always}",
    }


def _camera(node, config, project, destination, index, configuration_directory):
    reference = node.get("config_refs", {}).get("camera", "")
    camera = next(
        (
            cam
            for cam in config["cameras"]
            if cam.enabled and reference == f"cameras/{cam.id}"
        ),
        None,
    )
    if camera is None:
        raise ConfigError(
            "deployment", [f"node '{node['id']}' has no enabled camera reference"]
        )
    name = _service_name(node)
    if not name:
        raise ConfigError(
            "deployment", ["camera node cannot form a Compose service name"]
        )
    service = {
        "image": "visionengine-camera:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/camera/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": [
            "--config",
            "/config",
            "--camera-id",
            camera.id,
            "--bind",
            "tcp://0.0.0.0:5555",
            "--file-mode",
            "loop",
        ],
        "ports": [f"127.0.0.1:{5555 + index}:5555"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(configuration_directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
    }
    if isinstance(camera.source, int):
        device = f"/dev/video{camera.source}"
        service["devices"] = [f"{device}:{device}"]
    elif "://" not in camera.source:
        if "\\" in camera.source or PureWindowsPath(camera.source).drive:
            raise ConfigError(
                "deployment",
                [
                    f"camera '{camera.id}': video paths must use Linux-compatible paths, for example ./data/video.mp4"
                ],
            )
        original = Path(camera.source)
        source = (
            original.resolve()
            if original.is_absolute()
            else (project / original).resolve()
        )
        # Keep cameras.yaml unchanged: mount the video at the path the existing
        # source resolves to from the image's /app working directory.
        target = posixpath.normpath(posixpath.join("/app", camera.source))
        service["volumes"].append(
            {
                "type": "bind",
                "source": _relative(source, destination.parent),
                "target": target,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        )
    output = {
        "message_type": "frame.v1",
        "connect": f"tcp://{name}:5555",
        "host_connect": f"tcp://127.0.0.1:{5555 + index}",
        "topic": f"frame.v1/{camera.id}",
    }
    return name, service, output


# Build cameras first so inference can resolve its incoming graph endpoints.
# Downstream service types remain listed explicitly as omitted nodes.
SERVICE_BUILDERS = {"camera": _camera}


def _inference(
    node, config, project, destination, index, configuration_directory, outputs
):
    reference = node.get("config_refs", {}).get("model", "")
    model = next(
        (m for m in config.get("models", {}).values() if reference == f"models/{m.id}"),
        None,
    )
    if model is None:
        raise ConfigError("deployment", [f"node '{node['id']}' has no model reference"])
    name = _service_name(node)
    edges = [e for e in outputs["edges"] if e["to"] == node["id"] + ".frames"]
    if not edges:
        raise ConfigError(
            "deployment", [f"inference '{node['id']}' has no camera inputs"]
        )
    command = [
        "--config",
        "/config",
        "--model-id",
        model.id,
        "--bind",
        "tcp://0.0.0.0:5556",
        "--annotated-bind",
        "tcp://0.0.0.0:5557",
    ]
    dependencies, policies, seen = [], [], set()
    for edge in edges:
        output = outputs["frames"].get(edge["from"])
        if output is None or edge.get("message_type") != "frame.v1":
            raise ConfigError(
                "deployment",
                ["inference currently requires direct camera frame inputs"],
            )
        camera_id = output["topic"].split("/", 1)[1]
        camera = next(c for c in config["cameras"] if c.id == camera_id)
        if camera.model_id != model.id or camera_id in seen or edge.get("select"):
            raise ConfigError(
                "deployment", ["inference input assignment is invalid or duplicated"]
            )
        seen.add(camera_id)
        command.extend(["--input", camera_id + "=" + output["connect"]])
        dependencies.append(output["connect"].removeprefix("tcp://").split(":")[0])
        policies.append(edge["policy"])
    policy = policies[0]
    if (
        any(p != policy for p in policies)
        or policy["overflow"] != "drop_oldest"
        or policy["ordering"] != "camera"
    ):
        raise ConfigError(
            "deployment",
            [
                "shared inference currently requires matching drop_oldest, camera-ordered input policies"
            ],
        )
    command.extend(
        [
            "--max-items",
            str(policy["max_items"]),
            "--max-bytes",
            str(policy["max_bytes"]),
            "--max-age-ms",
            str(policy["max_age_ms"] or 0),
        ]
    )
    if "\\" in model.path or PureWindowsPath(model.path).drive:
        raise ConfigError(
            "deployment",
            [
                "model assets need Linux-compatible paths, for example ./models/people.pt"
            ],
        )
    original = Path(model.path)
    asset = (
        original.resolve() if original.is_absolute() else (project / original).resolve()
    )

    def mount(source, target):
        return {
            "type": "bind",
            "source": _relative(source, destination.parent),
            "target": target,
            "read_only": True,
            "bind": {"create_host_path": False},
        }

    service = {
        "image": "visionengine-inference:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/inference/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "command": command,
        "environment": _logging_environment(config),
        "ports": [f"127.0.0.1:{6000 + index}:5556", f"127.0.0.1:{7000 + index}:5557"],
        "volumes": [
            mount(configuration_directory, "/config"),
            mount(asset, posixpath.normpath(posixpath.join("/app", model.path))),
        ],
        "depends_on": dependencies,
    }
    # Device selection stays in models.yaml. Image dependencies and GPU access
    # are deployment-specific; the reference image deliberately uses CPU Torch.
    endpoints = {}
    for port, kind, internal, host in (
        ("detections", "detections.v1", 5556, 6000),
        ("annotated_frames", "frame.v1", 5557, 7000),
    ):
        endpoints[node["id"] + "." + port] = {
            "message_type": kind,
            "connect": f"tcp://{name}:{internal}",
            "host_connect": f"tcp://127.0.0.1:{host + index}",
            "topic": kind + "/<camera_id>",
        }
    return name, service, endpoints


def _tracking(
    node, config, project, destination, index, directory, pipeline, frames, inference
):
    refs = node.get("config_refs", {})
    camera = next(
        (
            c
            for c in config["cameras"]
            if c.enabled and refs.get("camera") == f"cameras/{c.id}"
        ),
        None,
    )
    if camera is None:
        raise ConfigError(
            "deployment", ["tracking node has no enabled camera reference"]
        )
    model = config["models"][camera.model_id]
    if (
        not model.use_tracker
        or refs.get("model") != f"models/{model.id}"
        or refs.get("tracker") != f"tracker_for/{camera.id}"
    ):
        raise ConfigError(
            "deployment",
            ["tracking node has inconsistent camera/model/tracker references"],
        )
    profile = config["tracker_profiles"][model.tracker]
    required = needs_frame(profile.algorithm, profile.params)
    name = _service_name(node)
    command = [
        "--config",
        "/config",
        "--camera-id",
        camera.id,
        "--bind",
        "tcp://0.0.0.0:5558",
    ]
    dependencies = []
    incoming = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if any(e["to"].rsplit(".", 1)[1] not in ("frames", "detections") for e in incoming):
        raise ConfigError("deployment", ["tracking input port is not implemented"])
    for port, endpoints, message, prefix in (
        ("detections", inference, "detections.v1", ""),
        ("frames", frames, "frame.v1", "frame-"),
    ):
        edges = [e for e in incoming if e["to"] == node["id"] + "." + port]
        expected = port == "detections" or required
        if len(edges) != int(expected):
            raise ConfigError(
                "deployment",
                [
                    f"tracking '{node['id']}' requires {int(expected)} {port} connection(s)"
                ],
            )
        if not edges:
            continue
        edge = edges[0]
        output = endpoints.get(edge["from"])
        source_node = next(
            (n for n in pipeline["nodes"] if n["id"] == edge["from"].rsplit(".", 1)[0]),
            {},
        )
        correct_source = (
            (
                source_node.get("config_refs", {}).get("model") == f"models/{model.id}"
                and edge.get("select") == {"camera_id": camera.id}
            )
            if port == "detections"
            else output and output["topic"] == f"frame.v1/{camera.id}"
        )
        if (
            not output
            or edge["message_type"] != message
            or output["message_type"] != message
            or not correct_source
        ):
            raise ConfigError(
                "deployment",
                ["tracking input does not match its assigned camera/model"],
            )
        policy = edge["policy"]
        if policy["overflow"] != "drop_oldest" or policy["ordering"] != "camera":
            raise ConfigError(
                "deployment",
                ["tracking inputs require drop_oldest, camera-ordered policies"],
            )
        command += [
            "--input" if port == "detections" else "--frames-input",
            output["connect"],
        ]
        for field in ("max_items", "max_bytes", "max_age_ms"):
            command += [
                "--" + prefix + field.replace("_", "-"),
                str(policy[field] or 0),
            ]
        dependencies.append(output["connect"].removeprefix("tcp://").split(":")[0])

    def mount(source, target):
        return {
            "type": "bind",
            "source": _relative(source, destination.parent),
            "target": target,
            "read_only": True,
            "bind": {"create_host_path": False},
        }

    volumes = [mount(directory, "/config")]
    if BOXMOT_ALGORITHMS[profile.algorithm].needs_reid(profile.params):
        weights = profile.reid["weights"]
        if "\\" in weights or PureWindowsPath(weights).drive:
            raise ConfigError(
                "deployment", ["ReID assets require Linux-compatible paths"]
            )
        asset = Path(weights)
        if not asset.is_absolute():
            asset = project / asset
        volumes.append(
            mount(asset.resolve(), posixpath.normpath(posixpath.join("/app", weights)))
        )
    service = {
        "image": "visionengine-tracking:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/tracking/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": command,
        "ports": [f"127.0.0.1:{8000 + index}:5558"],
        "volumes": volumes,
        "depends_on": list(dict.fromkeys(dependencies)),
    }
    output = {
        "message_type": "detections.v1",
        "connect": f"tcp://{name}:5558",
        "host_connect": f"tcp://127.0.0.1:{8000 + index}",
        "topic": f"detections.v1/{camera.id}",
    }
    return name, service, output


def _enrichment(
    node, config, project, destination, index, directory, pipeline, outputs
):
    reference = node.get("config_refs", {}).get("camera")
    camera = next(
        (c for c in config["cameras"] if c.enabled and reference == f"cameras/{c.id}"),
        None,
    )
    if camera is None:
        raise ConfigError(
            "deployment", ["enrichment node has no enabled camera reference"]
        )
    name = _service_name(node)
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if len(edges) != 1 or edges[0]["to"] != node["id"] + ".detections":
        raise ConfigError(
            "deployment", ["enrichment requires exactly one detections input"]
        )
    edge = edges[0]
    output = outputs.get(edge["from"])
    source = next(
        (n for n in pipeline["nodes"] if n["id"] == edge["from"].rsplit(".", 1)[0]), {}
    )
    refs = source.get("config_refs", {})
    correct_source = refs.get("model") == f"models/{camera.model_id}" and (
        (
            source.get("service") == "tracking"
            and refs.get("camera") == f"cameras/{camera.id}"
            and not edge.get("select")
        )
        or (
            source.get("service") == "inference"
            and edge.get("select") == {"camera_id": camera.id}
        )
    )
    if (
        not output
        or output["message_type"] != "detections.v1"
        or edge["message_type"] != "detections.v1"
        or not correct_source
    ):
        raise ConfigError(
            "deployment", ["enrichment input does not match its camera/model"]
        )
    policy = edge["policy"]
    if policy["overflow"] != "drop_oldest" or policy["ordering"] != "camera":
        raise ConfigError(
            "deployment",
            ["enrichment inputs require drop_oldest, camera-ordered policies"],
        )
    command = [
        "--config",
        "/config",
        "--camera-id",
        camera.id,
        "--input",
        output["connect"],
        "--bind",
        "tcp://0.0.0.0:5559",
    ]
    for field in ("max_items", "max_bytes", "max_age_ms"):
        command += ["--" + field.replace("_", "-"), str(policy[field] or 0)]
    service = {
        "image": "visionengine-enrichment:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/enrichment/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": command,
        "ports": [f"127.0.0.1:{9000 + index}:5559"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "depends_on": [output["connect"].removeprefix("tcp://").split(":")[0]],
    }
    endpoint = {
        "message_type": "event.v1",
        "connect": f"tcp://{name}:5559",
        "host_connect": f"tcp://127.0.0.1:{9000 + index}",
        "topic": f"event.v1/{camera.id}",
    }
    return name, service, endpoint


def _rules(node, config, project, destination, index, directory, pipeline, outputs):
    if node.get("config_refs", {}).get("rules") != "rules" or "rules" not in config:
        raise ConfigError(
            "deployment", ["rules node has no rules configuration reference"]
        )
    name = _service_name(node)
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if not edges:
        raise ConfigError(
            "deployment", ["rules requires at least one enrichment input"]
        )
    command = [
        "--config",
        "/config",
        "--instance-id",
        name,
        "--bind",
        "tcp://0.0.0.0:5560",
        "--alerts-bind",
        "tcp://0.0.0.0:5561",
    ]
    seen, policies, dependencies = set(), [], []
    for edge in edges:
        endpoint = outputs.get(edge["from"])
        if (
            edge["to"] != node["id"] + ".events"
            or not endpoint
            or edge["message_type"] != "event.v1"
            or edge.get("select")
        ):
            raise ConfigError(
                "deployment", ["rules requires direct enrichment event inputs"]
            )
        camera_id = endpoint["topic"].split("/", 1)[1]
        if camera_id in seen:
            raise ConfigError("deployment", ["rules camera input is duplicated"])
        seen.add(camera_id)
        command += ["--input", camera_id + "=" + endpoint["connect"]]
        dependencies.append(endpoint["connect"].removeprefix("tcp://").split(":")[0])
        policies.append(edge["policy"])
    policy = policies[0]
    if (
        any(p != policy for p in policies)
        or policy["overflow"] != "drop_oldest"
        or policy["ordering"] != "camera"
    ):
        raise ConfigError(
            "deployment",
            ["rules requires matching drop_oldest, camera-ordered input policies"],
        )
    for field in ("max_items", "max_bytes", "max_age_ms"):
        command += ["--" + field.replace("_", "-"), str(policy[field] or 0)]
    service = {
        "image": "visionengine-rules:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/rules/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": command,
        "ports": [f"127.0.0.1:{10000 + index}:5560", f"127.0.0.1:{11000 + index}:5561"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "depends_on": list(dict.fromkeys(dependencies)),
    }
    endpoints = {}
    for port, message, internal, host in (
        ("accepted", "event.v1", 5560, 10000),
        ("alerts", "alert.v1", 5561, 11000),
    ):
        endpoints[node["id"] + "." + port] = {
            "message_type": message,
            "connect": f"tcp://{name}:{internal}",
            "host_connect": f"tcp://127.0.0.1:{host + index}",
            "topic": message + "/<camera_id>",
        }
    return name, service, endpoints


def _row_builder(
    node, config, project, destination, index, directory, pipeline, outputs
):
    refs = node.get("config_refs", {})
    if refs.get("cameras") != "cameras" or refs.get("rules") != "rules":
        raise ConfigError(
            "deployment", ["row_builder requires cameras and rules references"]
        )
    name = _service_name(node)
    command = [
        "--config",
        "/config",
        "--instance-id",
        name,
        "--bind",
        "tcp://0.0.0.0:5562",
    ]
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    policies, dependencies, seen = [], [], set()
    has_events = False
    for edge in edges:
        port = edge["to"].rsplit(".", 1)[1]
        output = outputs.get(edge["from"])
        expected = {
            "events": "event.v1",
            "alerts": "alert.v1",
            "monitor": "health.v1",
        }.get(port)
        if port == "monitor" and refs.get("device") != "device":
            raise ConfigError(
                "deployment", ["row_builder monitor input requires device reference"]
            )
        if (
            not output
            or not expected
            or edge.get("select")
            or edge["message_type"] != expected
            or output["message_type"] != expected
        ):
            raise ConfigError(
                "deployment",
                ["row_builder requires direct rules or monitor outputs"],
            )
        identity = port, output["connect"]
        if identity in seen:
            raise ConfigError("deployment", ["row_builder input is duplicated"])
        seen.add(identity)
        has_events |= port == "events"
        command += [
            {
                "events": "--events-input",
                "alerts": "--alerts-input",
                "monitor": "--monitor-input",
            }[port],
            output["connect"],
        ]
        policies.append(edge["policy"])
        dependencies.append(output["connect"].removeprefix("tcp://").split(":")[0])
    if not has_events:
        raise ConfigError(
            "deployment", ["row_builder requires an accepted-event input"]
        )
    policy = policies[0]
    if (
        any(p != policy for p in policies)
        or policy["overflow"] != "drop_oldest"
        or policy["ordering"] != "camera"
    ):
        raise ConfigError(
            "deployment",
            [
                "row_builder requires matching drop_oldest, camera-ordered input policies"
            ],
        )
    for field in ("max_items", "max_bytes", "max_age_ms"):
        command += ["--" + field.replace("_", "-"), str(policy[field] or 0)]
    service = {
        "image": "visionengine-row-builder:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/row_builder/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": command,
        "ports": [f"127.0.0.1:{12000 + index}:5562"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "depends_on": list(dict.fromkeys(dependencies)),
    }
    endpoint = {
        "message_type": "rows.v1",
        "connect": f"tcp://{name}:5562",
        "host_connect": f"tcp://127.0.0.1:{12000 + index}",
        "topic": "rows.v1/<camera_id>",
        "device_topic": "rows.v1/device/<device_id>",
    }
    return name, service, endpoint


def _monitor(node, config, project, destination, directory):
    if node.get("config_refs", {}).get("device") != "device":
        raise ConfigError(
            "deployment", ["monitor requires a device configuration reference"]
        )
    name = _service_name(node)
    service = {
        "image": "visionengine-monitor:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/monitor/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": [
            "--config",
            "/config",
            "--instance-id",
            name,
            "--bind",
            "tcp://0.0.0.0:5563",
        ],
        "ports": ["127.0.0.1:13000:5563"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "volume",
                "source": "health_snapshots",
                "target": "/health",
                "read_only": True,
            },
        ],
    }
    health_file = config["device"].health_file
    if health_file.enabled:
        if PureWindowsPath(health_file.path).drive:
            raise ConfigError(
                "deployment",
                ["monitor health_file.path must use a portable Linux path"],
            )
        target = posixpath.normpath(posixpath.join("/app", health_file.path))
        parent = posixpath.dirname(target)
        if parent in (
            "/",
            "/app",
            "/config",
            "/health",
            "/tmp",
        ) or not parent.startswith(("/app/", "/data/")):
            raise ConfigError(
                "deployment",
                [
                    "monitor health_file.path must be inside /app/<directory> or /data/<directory>"
                ],
            )
        service["volumes"].append(
            {"type": "volume", "source": "monitor_reports", "target": parent}
        )
    endpoint = {
        "message_type": "health.v1",
        "connect": f"tcp://{name}:5563",
        "host_connect": "tcp://127.0.0.1:13000",
        "topic": "health.v1/" + config["device"].id,
    }
    return name, service, endpoint


def _monitor_watches(config, pipeline, services, monitor_name):
    """Share only health snapshots; monitor has no Docker socket or startup dependencies."""
    nodes = {n["id"]: n for n in pipeline["nodes"]}
    names = {key: _service_name(node) for key, node in nodes.items()}
    command = services[monitor_name]["command"]
    for node in nodes.values():
        if node["service"] == "buffer" and names[node["id"]] in services:
            command += ["--buffer-source", names[node["id"]]]
    for name, service in services.items():
        if name == monitor_name:
            continue
        kind = service["build"]["dockerfile"].split("/")[1]
        service["environment"][kind.upper() + "_HEALTH_FILE"] = f"/health/{name}.json"
        service["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-m",
                f"services.{kind}.runtime.healthcheck",
                "--file",
                f"/health/{name}.json",
            ],
            "interval": "10s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "120s"
            if kind in ("inference", "tracking")
            else "45s"
            if kind == "camera"
            else "30s",
        }
        service["volumes"].append(
            {"type": "volume", "source": "health_snapshots", "target": "/health"}
        )
        command += ["--watch", f"{name}=/health/{name}.json"]
    for camera in config["cameras"]:
        if not camera.enabled:
            continue
        required = set()
        for key, node in nodes.items():
            if names[key] not in services or node["service"] not in (
                "camera",
                "inference",
                "tracking",
                "enrichment",
            ):
                continue
            refs = node.get("config_refs", {})
            if refs.get("camera") == "cameras/" + camera.id or (
                node["service"] == "inference"
                and refs.get("model") == "models/" + camera.model_id
            ):
                required.add(names[key])
        counter_sources = set()
        for edge in pipeline["edges"]:
            origin = nodes.get(edge["from"].rsplit(".", 1)[0], {})
            target = nodes.get(edge["to"].rsplit(".", 1)[0], {})
            if (
                origin.get("config_refs", {}).get("camera") == "cameras/" + camera.id
                and target.get("service") == "rules"
            ):
                counter_sources.add(names[target["id"]])
        if len(counter_sources) > 1:
            raise ConfigError(
                "deployment",
                ["monitor currently requires one rules counter source per camera"],
            )
        for source in counter_sources:
            required.add(source)
            command += ["--camera-counter", camera.id + "=" + source]
        for source in sorted(required):
            command += ["--camera-stage", camera.id + "=" + source]


def _buffer(node, config, project, destination, directory):
    if node.get("config_refs", {}).get("buffer") != "api/buffer" or "api" not in config:
        raise ConfigError(
            "deployment", ["buffer requires api/buffer configuration reference"]
        )
    name = _service_name(node)
    path = config["api"].buffer.path
    if PureWindowsPath(path).drive:
        raise ConfigError("deployment", ["buffer path must use a portable Linux path"])
    target = posixpath.normpath(posixpath.join("/app", path))
    parent = posixpath.dirname(target)
    if parent in ("/app", "/", "/config", "/health") or not parent.startswith(
        ("/app/", "/data/", "/var/lib/")
    ):
        raise ConfigError(
            "deployment",
            [
                "buffer path must be inside a persistent /app/<directory>, /data/<directory> or /var/lib/<directory>"
            ],
        )
    service = {
        "image": "visionengine-buffer:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/buffer/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": _logging_environment(config),
        "command": [
            "--config",
            "/config",
            "--instance-id",
            name,
            "--bind",
            "tcp://0.0.0.0:5564",
            "--status-bind",
            "tcp://0.0.0.0:5565",
        ],
        "ports": ["127.0.0.1:14000:5564", "127.0.0.1:15000:5565"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {"type": "volume", "source": "buffer_data", "target": parent},
        ],
    }
    outputs = {
        node["id"] + ".pending": {
            "message_type": "buffer.batch.v1",
            "transport": "zmq_req_rep",
            "operation": "claim",
            "connect": f"tcp://{name}:5564",
            "host_connect": "tcp://127.0.0.1:14000",
        },
        node["id"] + ".status": {
            "message_type": "buffer.status.v1",
            "connect": f"tcp://{name}:5565",
            "host_connect": "tcp://127.0.0.1:15000",
            "topic": "buffer.status.v1/" + config["device"].id,
        },
    }
    return name, service, outputs


def _buffer_delivery(node, pipeline, services, name, volumes):
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if not edges:
        raise ConfigError(
            "deployment", ["buffer requires at least one row-builder input"]
        )
    nodes = {n["id"]: n for n in pipeline["nodes"]}
    for edge in edges:
        source = nodes.get(edge["from"].rsplit(".", 1)[0], {})
        policy = edge["policy"]
        if (
            source.get("service") != "row_builder"
            or edge["from"] != source["id"] + ".rows"
            or edge["to"] != node["id"] + ".rows"
            or edge["message_type"] != "rows.v1"
            or edge.get("select")
            or policy["overflow"] != "block"
            or policy["ordering"] != "none"
            or policy["max_age_ms"] is not None
        ):
            raise ConfigError(
                "deployment",
                [
                    "buffer requires direct row-builder inputs with block/none ordering and no age expiry"
                ],
            )
        source_name = _service_name(source)
        service = services.get(source_name)
        if service is None or "--buffer-connect" in service["command"]:
            raise ConfigError(
                "deployment",
                ["each deployed row builder requires one buffer destination"],
            )
        service["command"] += [
            "--buffer-connect",
            f"tcp://{name}:5564",
            "--delivery-path",
            "/delivery/pending.db",
            "--delivery-max-items",
            str(policy["max_items"]),
            "--delivery-max-bytes",
            str(policy["max_bytes"]),
        ]
        volume = "delivery_" + re.sub(r"[/_]+", "-", source["id"]).lower().strip(
            "-"
        ).replace("-", "_")
        volumes[volume] = {}
        service["volumes"].append(
            {"type": "volume", "source": volume, "target": "/delivery"}
        )
        service.setdefault("depends_on", []).append(name)


def _ingest(node, config, project, destination, directory, pipeline, outputs):
    if node.get("config_refs", {}).get("api") != "api":
        raise ConfigError("deployment", ["ingest requires api configuration reference"])
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if len(edges) != 1:
        raise ConfigError("deployment", ["ingest requires one buffer input"])
    edge = edges[0]
    output = outputs.get(edge["from"], {})
    policy = edge["policy"]
    if (
        edge["to"] != node["id"] + ".rows"
        or edge.get("select")
        or edge["message_type"] != "buffer.batch.v1"
        or output.get("transport") != "zmq_req_rep"
        or policy["overflow"] != "block"
        or policy["ordering"] != "none"
        or policy["max_age_ms"] is not None
    ):
        raise ConfigError(
            "deployment",
            [
                "ingest requires leased buffer batches with block/none ordering and no expiry"
            ],
        )
    if not 1 <= config["api"].ingest.batch_size <= min(1000, policy["max_items"]):
        raise ConfigError(
            "deployment",
            ["ingest batch_size exceeds the buffer edge limit (maximum 1000)"],
        )
    if not 1024 <= policy["max_bytes"] <= 16 * 1024 * 1024:
        raise ConfigError(
            "deployment", ["ingest claim size must be 1024 bytes to 16 MiB"]
        )
    name = _service_name(node)
    service = {
        "image": "visionengine-ingest:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/ingest/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "20s",
        "environment": _logging_environment(config),
        "command": [
            "--config",
            "/config",
            "--instance-id",
            name,
            "--buffer-connect",
            output["connect"],
            "--bind",
            "tcp://0.0.0.0:5566",
            "--claim-max-bytes",
            str(policy["max_bytes"]),
        ],
        "ports": ["127.0.0.1:16000:5566"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "depends_on": [output["connect"].removeprefix("tcp://").split(":")[0]],
    }
    endpoint = {
        "message_type": "ingest.status.v1",
        "connect": f"tcp://{name}:5566",
        "host_connect": "tcp://127.0.0.1:16000",
        "topic": "ingest.status.v1/" + config["device"].id,
    }
    return name, service, endpoint


def _notifier(node, config, project, destination, directory, pipeline, outputs):
    if node.get("config_refs", {}).get("notifications") != "notifications":
        raise ConfigError(
            "deployment", ["notifier requires notifications configuration reference"]
        )
    edges = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if not edges:
        raise ConfigError("deployment", ["notifier requires rules alert inputs"])
    name = _service_name(node)
    command = [
        "--config",
        "/config",
        "--instance-id",
        name,
        "--bind",
        "tcp://0.0.0.0:5567",
    ]
    dependencies, policies, seen = [], [], set()
    for edge in edges:
        output = outputs.get(edge["from"], {})
        if (
            edge["to"] != node["id"] + ".alerts"
            or edge.get("select")
            or edge["message_type"] != "alert.v1"
            or output.get("message_type") != "alert.v1"
            or not edge["from"].endswith(".alerts")
            or output.get("connect") in seen
        ):
            raise ConfigError(
                "deployment", ["notifier requires distinct direct rules.alerts inputs"]
            )
        command += ["--alerts-input", output["connect"]]
        seen.add(output["connect"])
        dependencies.append(output["connect"].removeprefix("tcp://").split(":")[0])
        policies.append(edge["policy"])
    policy = policies[0]
    if (
        any(p != policy for p in policies)
        or policy["overflow"] != "drop_oldest"
        or policy["ordering"] != "camera"
    ):
        raise ConfigError(
            "deployment",
            ["notifier inputs require matching drop_oldest, camera-ordered policies"],
        )
    if policy["max_items"] > 1000 or policy["max_bytes"] > 16 * 1024 * 1024:
        raise ConfigError(
            "deployment", ["notifier input limits exceed 1000 batches or 16 MiB"]
        )
    for key in ("max_items", "max_bytes", "max_age_ms"):
        command += ["--" + key.replace("_", "-"), str(policy[key] or 0)]
    environment = _logging_environment(config)
    for email in config["notifications"].emails:
        if email.enabled and email.smtp.password_env:
            variable = email.smtp.password_env
            if variable in environment or variable == "NOTIFIER_HEALTH_FILE":
                raise ConfigError(
                    "deployment",
                    ["SMTP password environment name conflicts with service settings"],
                )
            environment[variable] = (
                "${" + variable + ":?Set " + variable + " for notifier}"
            )
    service = {
        "image": "visionengine-notifier:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/notifier/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "15s",
        "environment": environment,
        "command": command,
        "ports": ["127.0.0.1:17000:5567"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ],
        "depends_on": list(dict.fromkeys(dependencies)),
    }
    endpoint = {
        "message_type": "notifier.status.v1",
        "connect": f"tcp://{name}:5567",
        "host_connect": "tcp://127.0.0.1:17000",
        "topic": "notifier.status.v1/" + config["device"].id,
    }
    return name, service, endpoint


def _collector(
    node, config, project, destination, index, directory, pipeline, frames, inference
):
    refs = node.get("config_refs", {})
    session = next(
        (
            s
            for s in config["collection"].sessions
            if s.enabled and refs.get("session") == "sessions/" + s.id
        ),
        None,
    )
    if session is None or refs.get("collection") != "collection":
        raise ConfigError(
            "deployment",
            ["collector requires enabled session and collection references"],
        )
    camera = next(
        (c for c in config["cameras"] if c.enabled and c.id == session.camera), None
    )
    if camera is None or refs.get("camera") != "cameras/" + camera.id:
        raise ConfigError(
            "deployment", ["collector camera reference differs from its session"]
        )
    name = _service_name(node)
    command = [
        "--config",
        "/config",
        "--session-id",
        session.id,
        "--instance-id",
        name,
        "--output-dir",
        "/data/collected",
        "--bind",
        "tcp://0.0.0.0:5568",
    ]
    incoming = [e for e in pipeline["edges"] if e["to"].rsplit(".", 1)[0] == node["id"]]
    if len(incoming) != 2:
        raise ConfigError(
            "deployment",
            ["collector requires one raw-frame and one inference-result input"],
        )
    dependencies = []
    for port, endpoints, kind, prefix in (
        ("frames", frames, "frame.v1", "frame-"),
        ("detections", inference, "detections.v1", ""),
    ):
        edges = [e for e in incoming if e["to"] == node["id"] + "." + port]
        if len(edges) != 1:
            raise ConfigError(
                "deployment", ["collector input ports must be frames and detections"]
            )
        edge = edges[0]
        output = endpoints.get(edge["from"], {})
        source = next(
            (n for n in pipeline["nodes"] if n["id"] == edge["from"].rsplit(".", 1)[0]),
            {},
        )
        correct = (
            (
                source.get("service") == "camera"
                and output.get("topic") == "frame.v1/" + camera.id
                and not edge.get("select")
            )
            if port == "frames"
            else (
                source.get("service") == "inference"
                and source.get("config_refs", {}).get("model")
                == "models/" + camera.model_id
                and edge.get("select") == {"camera_id": camera.id}
            )
        )
        if (
            not correct
            or edge["message_type"] != kind
            or output.get("message_type") != kind
        ):
            raise ConfigError(
                "deployment",
                [
                    "collector inputs must match its camera and direct inference model; regenerate the sample graph if it still uses tracking"
                ],
            )
        policy = edge["policy"]
        byte_limit = 512 * 1024 * 1024 if port == "frames" else 16 * 1024 * 1024
        if (
            policy["overflow"] != "drop_oldest"
            or policy["ordering"] != "camera"
            or policy["max_items"] > 1000
            or policy["max_bytes"] > byte_limit
        ):
            raise ConfigError(
                "deployment",
                [
                    "collector requires bounded drop_oldest, camera-ordered input policies"
                ],
            )
        command += [
            "--frames-input" if port == "frames" else "--detections-input",
            output["connect"],
        ]
        for field in ("max_items", "max_bytes", "max_age_ms"):
            command += [
                "--" + prefix + field.replace("_", "-"),
                str(policy[field] or 0),
            ]
        dependencies.append(output["connect"].removeprefix("tcp://").split(":")[0])
    raw = config["collection"].output_dir
    if "\\" in raw or PureWindowsPath(raw).drive or raw.startswith("~"):
        raise ConfigError(
            "deployment",
            [
                "collection output_dir must use a portable path such as ./collected or /mnt/datasets"
            ],
        )
    original = Path(raw)
    source = (original if original.is_absolute() else project / original).resolve()
    if (
        source == project
        or source == Path(source.anchor)
        or any(
            source.is_relative_to(project / reserved)
            for reserved in ("config", "services", "models", "tests", ".git", ".venv")
        )
    ):
        raise ConfigError(
            "deployment",
            [
                "collection output_dir must be a dedicated data directory outside source/configuration folders"
            ],
        )
    if source.exists() and not source.is_dir():
        raise ConfigError("deployment", ["collection output_dir is not a directory"])
    service = {
        "image": "visionengine-collector:local",
        "build": {
            "context": _relative(project, destination.parent),
            "dockerfile": "services/collector/Dockerfile",
        },
        "init": True,
        "restart": "unless-stopped",
        "stop_grace_period": "25s",
        "environment": _logging_environment(config),
        "command": command,
        "ports": [f"127.0.0.1:{18000 + index}:5568"],
        "volumes": [
            {
                "type": "bind",
                "source": _relative(directory, destination.parent),
                "target": "/config",
                "read_only": True,
                "bind": {"create_host_path": False},
            },
            {
                "type": "bind",
                "source": _relative(source, destination.parent),
                "target": "/data/collected",
                "bind": {"create_host_path": True},
            },
        ],
        "depends_on": list(dict.fromkeys(dependencies)),
    }
    endpoint = {
        "message_type": "collector.status.v1",
        "connect": f"tcp://{name}:5568",
        "host_connect": f"tcp://127.0.0.1:{18000 + index}",
        "topic": "collector.status.v1/" + session.id,
    }
    return name, service, endpoint


def compile_deployment(
    config, pipeline, destination: Path, *, project: Path | None = None
) -> Deployment:
    project = (project or Path(__file__).resolve().parents[2]).resolve()
    destination = destination.resolve()
    configuration_directory = Path(
        pipeline.get("configuration_directory", project / "config/YAMLs")
    ).resolve()
    services, outputs = {}, {}
    omitted = []
    for node in pipeline["nodes"]:
        if node["service"] in (
            "inference",
            "tracking",
            "enrichment",
            "rules",
            "row_builder",
            "monitor",
            "buffer",
            "ingest",
            "notifier",
            "collector",
        ):
            continue
        builder = SERVICE_BUILDERS.get(node["service"])
        if builder is None:
            omitted.append(node["id"])
            continue
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment",
                ["camera deployment currently supports only the local device"],
            )
        name, service, output = builder(
            node, config, project, destination, len(services), configuration_directory
        )
        if name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        if 5555 + len(services) > 65535:
            raise ConfigError(
                "deployment",
                ["camera deployment exceeds the available host port range"],
            )
        services[name] = service
        outputs[node["id"] + ".frames"] = output
    if not services:
        raise ConfigError("deployment", ["no camera nodes to deploy in this pipeline"])
    inference_outputs = {}
    inference_nodes = [n for n in pipeline["nodes"] if n["service"] == "inference"]
    if len(outputs) > 445 or len(inference_nodes) > 1000:
        raise ConfigError(
            "deployment",
            ["deployment exceeds the reserved camera/inference host port ranges"],
        )
    for index, node in enumerate(inference_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment",
                ["inference deployment currently supports only the local device"],
            )
        name, service, endpoints = _inference(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            {"frames": outputs, "edges": pipeline["edges"]},
        )
        if not name or name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        services[name] = service
        inference_outputs.update(endpoints)
    tracking_outputs = {}
    tracking_nodes = [n for n in pipeline["nodes"] if n["service"] == "tracking"]
    if len(tracking_nodes) > 1000:
        raise ConfigError(
            "deployment", ["tracking deployment exceeds its reserved host port range"]
        )
    for index, node in enumerate(tracking_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment",
                ["tracking deployment currently supports only the local device"],
            )
        name, service, output = _tracking(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            pipeline,
            outputs,
            inference_outputs,
        )
        if not name or name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        services[name] = service
        tracking_outputs[node["id"] + ".tracks"] = output
    enrichment_outputs = {}
    enrichment_nodes = [n for n in pipeline["nodes"] if n["service"] == "enrichment"]
    if len(enrichment_nodes) > 1000:
        raise ConfigError(
            "deployment", ["enrichment deployment exceeds its reserved host port range"]
        )
    for index, node in enumerate(enrichment_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment",
                ["enrichment deployment currently supports only the local device"],
            )
        name, service, endpoint = _enrichment(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            pipeline,
            {**inference_outputs, **tracking_outputs},
        )
        if not name or name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        services[name] = service
        enrichment_outputs[node["id"] + ".events"] = endpoint
    rules_outputs = {}
    rules_nodes = [n for n in pipeline["nodes"] if n["service"] == "rules"]
    if len(rules_nodes) > 1000:
        raise ConfigError(
            "deployment", ["rules deployment exceeds its reserved host port ranges"]
        )
    for index, node in enumerate(rules_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment",
                ["rules deployment currently supports only the local device"],
            )
        name, service, endpoints = _rules(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            pipeline,
            enrichment_outputs,
        )
        if not name or name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        services[name] = service
        rules_outputs.update(endpoints)
    monitor_outputs = {}
    monitor_nodes = [n for n in pipeline["nodes"] if n["service"] == "monitor"]
    if len(monitor_nodes) > 1:
        raise ConfigError(
            "deployment", ["only one monitor per local device is supported"]
        )
    monitor_name = None
    for node in monitor_nodes:
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["monitor deployment supports only the local device"]
            )
        monitor_name, service, endpoint = _monitor(
            node, config, project, destination, configuration_directory
        )
        if not monitor_name or monitor_name in services:
            raise ConfigError("deployment", ["monitor service name collides"])
        services[monitor_name] = service
        monitor_outputs[node["id"] + ".health"] = endpoint
    row_outputs = {}
    row_nodes = [n for n in pipeline["nodes"] if n["service"] == "row_builder"]
    if len(row_nodes) > 1000:
        raise ConfigError(
            "deployment", ["row_builder exceeds its reserved host port range"]
        )
    for index, node in enumerate(row_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["row_builder deployment supports only the local device"]
            )
        name, service, endpoint = _row_builder(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            pipeline,
            {**rules_outputs, **monitor_outputs},
        )
        if not name or name in services:
            raise ConfigError(
                "deployment",
                [f"Compose service name collision for node '{node['id']}'"],
            )
        services[name] = service
        row_outputs[node["id"] + ".rows"] = endpoint
    buffer_outputs = {}
    persistent_volumes = {}
    buffer_nodes = [n for n in pipeline["nodes"] if n["service"] == "buffer"]
    if len(buffer_nodes) > 1:
        raise ConfigError(
            "deployment", ["only one buffer per local device is supported"]
        )
    for node in buffer_nodes:
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["buffer deployment supports only the local device"]
            )
        name, service, endpoints = _buffer(
            node, config, project, destination, configuration_directory
        )
        if not name or name in services:
            raise ConfigError("deployment", ["buffer service name collides"])
        services[name] = service
        buffer_outputs.update(endpoints)
        persistent_volumes["buffer_data"] = {}
        _buffer_delivery(node, pipeline, services, name, persistent_volumes)
    ingest_outputs = {}
    ingest_nodes = [n for n in pipeline["nodes"] if n["service"] == "ingest"]
    if len(ingest_nodes) > 1:
        raise ConfigError(
            "deployment", ["only one ingest per local device is supported"]
        )
    for node in ingest_nodes:
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["ingest deployment supports only the local device"]
            )
        name, service, endpoint = _ingest(
            node,
            config,
            project,
            destination,
            configuration_directory,
            pipeline,
            buffer_outputs,
        )
        if not name or name in services:
            raise ConfigError("deployment", ["ingest service name collides"])
        services[name] = service
        ingest_outputs[node["id"] + ".status"] = endpoint
    notifier_outputs = {}
    notifier_nodes = [n for n in pipeline["nodes"] if n["service"] == "notifier"]
    if len(notifier_nodes) > 1:
        raise ConfigError(
            "deployment", ["only one notifier per local device is supported"]
        )
    for node in notifier_nodes:
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["notifier deployment supports only the local device"]
            )
        name, service, endpoint = _notifier(
            node,
            config,
            project,
            destination,
            configuration_directory,
            pipeline,
            rules_outputs,
        )
        if not name or name in services:
            raise ConfigError("deployment", ["notifier service name collides"])
        services[name] = service
        notifier_outputs[node["id"] + ".status"] = endpoint
    collector_outputs = {}
    collector_nodes = [n for n in pipeline["nodes"] if n["service"] == "collector"]
    if len(collector_nodes) > 1000:
        raise ConfigError(
            "deployment", ["collector deployment exceeds its reserved host port range"]
        )
    for index, node in enumerate(collector_nodes):
        if node.get("placement", {}).get("device") != config["device"].id:
            raise ConfigError(
                "deployment", ["collector deployment supports only the local device"]
            )
        name, service, endpoint = _collector(
            node,
            config,
            project,
            destination,
            index,
            configuration_directory,
            pipeline,
            outputs,
            inference_outputs,
        )
        if not name or name in services:
            raise ConfigError("deployment", ["collector service name collides"])
        services[name] = service
        collector_outputs[node["id"] + ".status"] = endpoint
    stage = ["camera"]
    if inference_nodes:
        stage.append("inference")
    if tracking_nodes:
        stage.append("tracking")
    if enrichment_nodes:
        stage.append("enrichment")
    if rules_nodes:
        stage.append("rules")
    if row_nodes:
        stage.append("row_builder")
    if buffer_nodes:
        stage.append("buffer")
    if ingest_nodes:
        stage.append("ingest")
    if notifier_nodes:
        stage.append("notifier")
    if collector_nodes:
        stage.append("collector")
    if monitor_nodes:
        stage.append("monitor")
        _monitor_watches(config, pipeline, services, monitor_name)
    compose = {
        "name": f"visionengine-{config['device'].environment}-edge",
        "x-visionengine": {
            "stage": "+".join(stage),
            "configuration_revision": pipeline["configuration_revision"],
            "omitted_nodes": omitted,
            "frame_outputs": outputs,
            "inference_outputs": inference_outputs,
            "tracking_outputs": tracking_outputs,
            "enrichment_outputs": enrichment_outputs,
            "rules_outputs": rules_outputs,
            "row_outputs": row_outputs,
            "monitor_outputs": monitor_outputs,
            "buffer_outputs": buffer_outputs,
            "ingest_outputs": ingest_outputs,
            "notifier_outputs": notifier_outputs,
            "collector_outputs": collector_outputs,
        },
        "services": services,
    }
    if monitor_nodes:
        compose["volumes"] = {"health_snapshots": {}}
        if config["device"].health_file.enabled:
            compose["volumes"]["monitor_reports"] = {}
    if persistent_volumes:
        compose.setdefault("volumes", {}).update(persistent_volumes)
    return Deployment(compose)


def split_compose(document):
    """Keep deployment semantics; group containers by service Dockerfile."""
    fragments = {}
    for name, service in document["services"].items():
        kind = service["build"]["dockerfile"].split("/")[1]
        filename = f"{kind}.compose.yaml"
        fragments.setdefault(filename, {"services": {}})["services"][name] = service
    umbrella = {key: value for key, value in document.items() if key != "services"}
    umbrella["include"] = list(fragments)
    # An empty deployment still has a valid Compose services mapping.
    if not fragments:
        umbrella["services"] = {}
    return umbrella, fragments


def dump_compose(document) -> str:
    """Separate generated service blocks and comment their deployment settings."""
    content = dump_yaml(document)
    tree = yaml.compose(content)
    comments = {}
    descriptions = {
        "image": "Image name shared by all instances of this service type.",
        "build": "Build from the independent project using this service's Dockerfile.",
        "init": "Process lifecycle and restart behavior.",
        "command": "Arguments appended to the image ENTRYPOINT.",
        "environment": "Console/JSON logs, severity threshold and terminal colors; override with host environment variables.",
        "ports": "Host address:host port:container port; host access is local by default.",
        "volumes": "Configuration, assets and persistent data mounts.",
        "depends_on": "Start upstream containers first; inputs reconnect independently.",
        "devices": "Linux USB device passthrough; requires the device on this host.",
        "healthcheck": "Read the same unique health snapshot that the monitor observes.",
    }
    for key, value in tree.value:
        if key.value == "services":
            for service_key, service in value.value:
                comments[service_key.start_mark.line] = (
                    service_key.start_mark.column,
                    "One container for this configured node.",
                )
                for field, _ in service.value:
                    if field.value in descriptions:
                        comments[field.start_mark.line] = (
                            field.start_mark.column,
                            descriptions[field.value],
                        )
    lines = [
        "# Generated by config.controller --generate; edit configuration, then regenerate.",
        "# Implemented camera/inference/tracking/enrichment/rules/row_builder/buffer/ingest/notifier/collector/monitor nodes; see omitted_nodes for planned services.",
    ]
    for index, line in enumerate(content.splitlines()):
        if index in comments:
            indent, comment = comments[index]
            if lines[-1].strip():
                lines.append("")
            lines.append(" " * indent + "# " + comment)
        lines.append(line)
    return "\n".join(lines) + "\n"
