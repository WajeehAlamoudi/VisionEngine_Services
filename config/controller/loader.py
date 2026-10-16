"""Read registered files, parse each independently, then run cross-file checks."""

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .registry import SECTIONS, Section
from .validation import validate_config
from .parsers.tracker import parse as parse_tracker
from .parsers.strict import ConfigError


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently overwriting settings."""


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                None, None, "mapping keys must be scalar values", key_node.start_mark
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate mapping key", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def load_file(path: Path, section: str | None, *, allow_empty: bool = False) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(path.name, ["cannot read configuration file"]) from exc
    try:
        document = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        # Do not print YAML source lines: configuration can contain credentials.
        raise ConfigError(path.name, [f"invalid YAML{location}"]) from exc
    if document is None and allow_empty:
        return None
    if not isinstance(document, dict):
        raise ConfigError(path.name, ["expected a top-level mapping"])
    if section is None:
        return document
    if set(document) != {section}:
        raise ConfigError(
            path.name, [f"expected only the '{section}' top-level section"]
        )
    return document[section]


def load_config(
    directory: str | Path,
    *,
    sections: Sequence[Section] = SECTIONS,
    checks: Sequence[Callable[[Mapping[str, Any]], None]] | None = None,
    samples: bool = False,
) -> dict[str, Any]:
    """Return typed, locally validated settings, not a compiled execution graph.

    Cross-file checks run only after all selected files have parsed successfully.
    Each check raises ConfigError for its semantic errors. No files are written.
    Hardware and artifact availability are deferred to deployment preflight.
    """
    names = [section.name for section in sections]
    if len(names) != len(set(names)):
        raise ValueError("configuration section names must be unique")
    result = {}
    problems = []
    for section in sections:
        filename = Path(section.filename)
        if filename.name != section.filename or filename.is_absolute():
            raise ValueError(
                "registered configuration filenames must be plain filenames"
            )
        if samples:
            filename = filename.with_name(filename.stem + ".sample" + filename.suffix)
        path = Path(directory) / filename
        try:
            key = None if section.whole_document else (section.root_key or section.name)
            result[section.name] = section.parse(
                load_file(path, key, allow_empty=section.allow_empty)
            )
        except ConfigError as exc:
            problems.extend(f"{path.name}: {problem}" for problem in exc.problems)
    if problems:
        raise ConfigError("configuration", problems)
    for check in (validate_config,) if checks is None else checks:
        try:
            check(result)
        except ConfigError as exc:
            problems.extend(f"{exc.filename}: {problem}" for problem in exc.problems)
    if problems:
        raise ConfigError("configuration", problems)
    if {"models", "cameras", "tracker"} <= result.keys():
        result["tracker_profiles"] = _tracker_profiles(result, Path(directory), samples)
    return result


def _tracker_profiles(
    config: Mapping[str, Any], directory: Path, samples: bool
) -> dict:
    """Validate referenced BoxMOT profiles inside this configuration directory.

    The standard config/boxmot_tracker.yaml reference maps to the registered
    profile, including in sample mode. Other config-relative profiles are read
    and parsed here rather than silently assigned the standard profile.
    """
    profiles = {}
    problems = []
    root = directory.resolve()
    for camera in config["cameras"]:
        model = config["models"][camera.model_id]
        if not camera.enabled or not model.use_tracker or model.tracker in profiles:
            continue
        reference = Path(model.tracker.replace("\\", "/"))
        if reference.as_posix() in (
            "boxmot_tracker.yaml",
            "config/boxmot_tracker.yaml",
        ):
            profiles[model.tracker] = config["tracker"]
            continue
        if reference.parts and reference.parts[0] == "config":
            reference = Path(*reference.parts[1:])
        path = (root / reference).resolve()
        if not path.is_relative_to(root):
            problems.append(
                f"models[{model.id}].tracker: profile must be inside the configuration directory"
            )
            continue
        if samples:
            path = path.with_name(path.stem + ".sample" + path.suffix)
        try:
            profiles[model.tracker] = parse_tracker(load_file(path, None))
        except ConfigError as exc:
            problems.extend(f"{path.name}: {problem}" for problem in exc.problems)
    if problems:
        raise ConfigError("tracker references", problems)
    return profiles
