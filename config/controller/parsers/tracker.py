"""Parse BoxMOT settings without importing a tracker, Torch, or GPU runtime."""

from dataclasses import dataclass
import logging
import math
import re
from typing import Any

from .strict import ConfigError
from .tracker_algorithms import BOXMOT_ALGORITHMS, resolve_params

log = logging.getLogger(__name__)
_FILE = "boxmot_tracker.yaml"
_REID_DEFAULTS = {
    "weights": "osnet_x0_25_msmt17.pt",
    "backend": "pytorch",
    "device": "auto",
    "half": False,
}


@dataclass
class TrackerConfig:
    algorithm: str
    params: dict[str, Any]
    reid: dict[str, Any]


def _mapping(raw: Any, path: str) -> dict:
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ConfigError(_FILE, [f"{path}: expected a mapping with string keys"])
    return raw


def _algorithm(raw: Any) -> str:
    if not isinstance(raw, str) or raw.strip().lower() not in BOXMOT_ALGORITHMS:
        raise ConfigError(_FILE, ["algorithm: unsupported BoxMOT algorithm"])
    return raw.strip().lower()


def _params(algorithm: str, common: dict, configured: dict) -> dict:
    try:
        params = resolve_params(algorithm, common, configured)
    except RuntimeError as exc:
        raise ConfigError(_FILE, [str(exc)]) from exc
    problems = []
    for key, value in params.items():
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"params.{key}: expected a finite number")
    for key in ("frame_rate", "max_age", "max_obs", "min_hits", "track_buffer"):
        if key in params and params[key] <= 0:
            problems.append(f"params.{key}: expected a positive number")
    # Registry entries with None defaults represent optional integer dimensions.
    for key in ("frame_width", "frame_height", "horizontal_margin", "vertical_margin"):
        value = params.get(key)
        if value is not None and (type(value) is not int or value < 0):
            problems.append(f"params.{key}: expected a non-negative integer or null")
    if problems:
        raise ConfigError(_FILE, problems)
    return params


def parse(raw: Any) -> TrackerConfig:
    raw = dict(_mapping(raw, "tracker"))
    modern = any(key in raw for key in ("common", "params", "algorithms", "reid"))
    if modern:
        if set(raw) - {"algorithm", "common", "params", "algorithms", "reid"}:
            raise ConfigError(_FILE, ["unknown tracker field"])
        algorithm = _algorithm(raw.get("algorithm", "bytetrack"))
        common = _mapping(raw.get("common", {}), "common")
        if "params" in raw and "algorithms" in raw:
            raise ConfigError(_FILE, ["use params or algorithms, not both"])
        if "algorithms" in raw:
            profiles = _mapping(raw["algorithms"], "algorithms")
            if algorithm not in profiles:
                raise ConfigError(_FILE, ["selected algorithm profile is missing"])
            for name, values in profiles.items():
                _params(
                    _algorithm(name), common, _mapping(values, f"algorithms.{name}")
                )
            configured = profiles[algorithm]
            log.warning("%s: algorithms layout is deprecated; use params", _FILE)
        else:
            configured = _mapping(raw.get("params", {}), "params")
        reid = _mapping(raw.get("reid", {}), "reid")
    else:
        # Preserve the existing engine's older flat BoT-SORT configuration.
        algorithm = _algorithm(raw.pop("algorithm", "botsort"))
        reid = {
            key: raw.pop("reid_" + key, value) for key, value in _REID_DEFAULTS.items()
        }
        common, configured = {}, raw
        log.warning(
            "%s: flat tracker layout is deprecated; use common/params/reid", _FILE
        )
    params = _params(algorithm, common, configured)
    if set(reid) - set(_REID_DEFAULTS):
        raise ConfigError(_FILE, ["reid: unknown field"])
    reid = {**_REID_DEFAULTS, **reid}
    problems = []
    for key in ("weights", "backend", "device"):
        if not isinstance(reid[key], str) or not reid[key].strip():
            problems.append(f"reid.{key}: expected a non-empty string")
    if not isinstance(reid["half"], bool):
        problems.append("reid.half: expected true or false")
    if reid["backend"] not in ("pytorch", "tensorrt"):
        problems.append("reid.backend: expected pytorch or tensorrt")
    if not isinstance(reid["device"], str) or not re.fullmatch(
        r"auto|cpu|cuda(?::\d+)?|mps", reid["device"]
    ):
        problems.append("reid.device: unsupported device")
    if problems:
        raise ConfigError(_FILE, problems)
    return TrackerConfig(algorithm, params, reid)
