"""Expand a configured graph into a concrete, validated planning artifact."""

from collections import deque
from dataclasses import asdict
import hashlib
import json
from typing import Any, Mapping
import re

from .parsers.graph import GraphConfig
from .parsers.strict import ConfigError
from .parsers.tracker_algorithms import needs_frame


def _error(message):
    raise ConfigError("graph.yaml", [message])


def _contexts(config):
    cameras = [c for c in config["cameras"] if c.enabled]
    models = config["models"]
    device = config["device"]
    notifications = config["notifications"]
    base = {
        "device.id": device.id,
        "always": True,
        "notifications_enabled": notifications.log.enabled
        or any(w.enabled for w in notifications.webhooks)
        or any(e.enabled for e in notifications.emails),
    }
    camera_contexts = [
        {
            **base,
            "camera.id": c.id,
            "camera.model_id": c.model_id,
            "model.id": c.model_id,
            "tracking_enabled": models[c.model_id].use_tracker,
            "tracking_disabled": not models[c.model_id].use_tracker,
            "tracking_requires_frame": models[c.model_id].use_tracker
            and needs_frame(
                config["tracker_profiles"][models[c.model_id].tracker].algorithm,
                config["tracker_profiles"][models[c.model_id].tracker].params,
            ),
        }
        for c in cameras
    ]
    by_camera = {c["camera.id"]: c for c in camera_contexts}
    sessions = []
    for session in config["collection"].sessions:
        if session.enabled and session.camera in by_camera:
            sessions.append(
                {
                    **by_camera[session.camera],
                    "session.id": session.id,
                    "session.camera": session.camera,
                }
            )
    return base, {
        "enabled_cameras": camera_contexts,
        "used_models": [
            {**base, "model.id": model_id}
            for model_id in sorted({c.model_id for c in cameras})
        ],
        "tracked_cameras": [c for c in camera_contexts if c["tracking_enabled"]],
        "untracked_cameras": [c for c in camera_contexts if not c["tracking_enabled"]],
        "enabled_collection_sessions": sessions,
    }


def _expand(value, context):
    def substitute(match):
        key = match.group(1)
        if key not in context:
            _error(f"placeholder '{key}' is unavailable in this expansion")
        return str(context[key])

    return re.sub(r"\{([^{}]+)\}", substitute, value)


def _instances(template, base, groups):
    if not template.get("enabled", True):
        return
    contexts = groups[template["for_each"]] if "for_each" in template else [base]
    condition = template.get("when", "always")
    for context in contexts:
        if condition not in context:
            _error(
                f"condition '{condition}' is unavailable for template '{template['id']}'"
            )
        if context[condition]:
            yield context


def _reference_exists(reference, config):
    # References are semantic identifiers, never Python expressions or filenames.
    parts = reference.split("/")
    root = parts[0]
    if len(parts) == 1:
        return root in config
    if len(parts) != 2:
        return False
    name = parts[1]
    if root == "cameras":
        return any(camera.id == name for camera in config["cameras"])
    if root == "models":
        return name in config["models"]
    if root == "sessions":
        return any(session.id == name for session in config["collection"].sessions)
    if root == "tracker_for":
        camera = next((c for c in config["cameras"] if c.id == name), None)
        return camera is not None and config["models"][
            camera.model_id
        ].tracker in config.get("tracker_profiles", {})
    if root == "api":
        return name in ("buffer", "ingest", "request")
    return False


def _port(endpoint, nodes, contracts, direction):
    if "." not in endpoint:
        _error(f"endpoint '{endpoint}' must be node_id.port")
    node_id, port = endpoint.rsplit(".", 1)
    if node_id not in nodes:
        _error(f"endpoint '{endpoint}' references a missing or disabled node")
    ports = contracts[nodes[node_id]["service"]].get(direction, {})
    if port not in ports:
        _error(f"endpoint '{endpoint}' is not a declared {direction} port")
    return node_id, port, ports[port]


def compile_pipeline(config: Mapping[str, Any], graph: GraphConfig) -> dict:
    """Compile validated settings and template declarations; never write files.

    Output is a plan only. It does not promise executable service implementations,
    actual model sharing, queue enforcement, hardware checks, or Compose deployment.
    Config references preserve resource bindings without serializing credentials.
    """
    base, groups = _contexts(config)
    nodes = {}
    edges = []
    edge_ids = set()
    endpoint_pairs = set()
    for template in graph.nodes:
        for context in _instances(template, base, groups):
            node_id = _expand(template["id"], context)
            if not re.fullmatch(r"[A-Za-z0-9_/-]+", node_id):
                _error(f"invalid node ID '{node_id}'")
            if node_id in nodes:
                _error(f"duplicate node ID '{node_id}'")
            refs = {
                key: _expand(value, context)
                for key, value in template.get("config_refs", {}).items()
            }
            for reference in refs.values():
                if not _reference_exists(reference, config):
                    _error(
                        f"node '{node_id}' has unknown configuration reference '{reference}'"
                    )
            nodes[node_id] = {
                "id": node_id,
                "service": template["service"],
                "placement": {"device": config["device"].id},
                "config_refs": refs,
            }
            if template["service"] == "tracking":
                camera = next(
                    (
                        c
                        for c in config["cameras"]
                        if c.enabled and refs.get("camera") == f"cameras/{c.id}"
                    ),
                    None,
                )
                if camera is None or not config["models"][camera.model_id].use_tracker:
                    _error(
                        f"tracking node '{node_id}' needs a camera with tracking enabled"
                    )
                model = config["models"][camera.model_id]
                profile = config["tracker_profiles"][model.tracker]
                nodes[node_id]["requires_frame"] = needs_frame(
                    profile.algorithm, profile.params
                )
    for template in graph.edges:
        for context in _instances(template, base, groups):
            edge_id = _expand(template["id"], context)
            if not re.fullmatch(r"[A-Za-z0-9_/-]+", edge_id):
                _error(f"invalid edge ID '{edge_id}'")
            if edge_id in edge_ids:
                _error(f"duplicate edge ID '{edge_id}'")
            edge_ids.add(edge_id)
            source = _expand(template["from"], context)
            target = _expand(template["to"], context)
            select = {
                key: _expand(value, context)
                for key, value in template.get("select", {}).items()
            }
            pair = (source, target, tuple(sorted(select.items())))
            if pair in endpoint_pairs:
                _error(f"duplicate connection '{source}' -> '{target}'")
            endpoint_pairs.add(pair)
            src, _, source_type = _port(source, nodes, graph.services, "outputs")
            dst, port, target_type = _port(target, nodes, graph.services, "inputs")
            if source_type != target_type:
                _error(
                    f"incompatible connection '{source}' ({source_type}) -> '{target}' ({target_type})"
                )
            if select and source_type != "detections.v1":
                _error(
                    f"camera selection is only supported on detection batches: '{edge_id}'"
                )
            if select.get("camera_id") not in (
                None,
                *[c.id for c in config["cameras"] if c.enabled],
            ):
                _error(f"edge '{edge_id}' selects a missing or disabled camera")
            profile = dict(graph.profiles[template["profile"]])
            profile.setdefault("max_age_ms", None)
            profile.setdefault("ordering", "none")
            edges.append(
                {
                    "id": edge_id,
                    "from": source,
                    "to": target,
                    "message_type": source_type,
                    "profile": template["profile"],
                    "policy": profile,
                    "select": select,
                }
            )
    incoming = {
        (edge["to"].rsplit(".", 1)[0], edge["to"].rsplit(".", 1)[1]) for edge in edges
    }
    for node in nodes.values():
        spec = graph.services[node["service"]]
        required = set(spec.get("required_inputs", list(spec.get("inputs", {}))))
        if node["service"] == "tracking":
            required.discard("frames")
            if node["requires_frame"]:
                required.add("frames")
        for port in required:
            if (node["id"], port) not in incoming:
                _error(
                    f"node '{node['id']}' has no connection for required input '{port}'"
                )
    # Reject data-flow cycles; acknowledgement/control channels are not modeled yet.
    degree = dict.fromkeys(nodes, 0)
    outgoing = {node: [] for node in nodes}
    for edge in edges:
        src = edge["from"].rsplit(".", 1)[0]
        dst = edge["to"].rsplit(".", 1)[0]
        outgoing[src].append(dst)
        degree[dst] += 1
    ready = deque(node for node, count in degree.items() if count == 0)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for child in outgoing[node]:
            degree[child] -= 1
            if degree[child] == 0:
                ready.append(child)
    if visited != len(nodes):
        _error("data-flow graph contains a cycle")
    revision = hashlib.sha256(
        json.dumps(config, default=asdict, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "version": 1,
        "status": "planned",
        "configuration_revision": revision,
        "device": config["device"].id,
        "nodes": list(nodes.values()),
        "edges": edges,
    }
