from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .strict import ID_PATTERN, Reader, describe

_FILE = "models.yaml"
_MODEL_KEYS = (
    "id",
    "name",
    "version",
    "path",
    "runtime",
    "accelerator",
    "classes",
    "confidence_threshold",
    "iou_threshold",
    "input_size",
    "use_tracker",
    "tracker",
    "half",
    "ds_infer_config",
)
RUNTIMES = ("ultralytics",)
SELF_CAPTURING_RUNTIMES = ("deepstream",)
ACCELERATORS = ("auto", "cpu", "cuda", "mps", "coreml")
_FAMILIAR_FORMATS = {".pt", ".onnx", ".engine", ".mlpackage", ".tflite"}
_DEVICE_MIGRATION = {
    "auto": ("ultralytics", "auto"),
    "cpu": ("ultralytics", "cpu"),
    "cuda": ("ultralytics", "cuda"),
    "mps": ("ultralytics", "mps"),
    "coreml": ("ultralytics", "coreml"),
    "deepstream": ("deepstream", "cuda"),
}
_STRIDE = 32


@dataclass
class ModelConfig:
    id: str
    name: str
    version: str
    path: str
    runtime: str
    accelerator: str
    classes: list[str]
    confidence_threshold: float
    iou_threshold: float
    input_size: list[int]
    use_tracker: bool
    tracker: str
    half: bool
    ds_infer_config: str | None = None


def needs_model_runner(model: "ModelConfig") -> bool:
    """
    Whether this model is executed through the shared model layer.

    False for a runtime that owns its own capture and inference. Answered from
    config alone so neither the model layer nor the runtime layer has to import
    the other to find out.
    """
    return model.runtime not in SELF_CAPTURING_RUNTIMES


def _check_format(r: Reader, path_value: str, runtime: str, accelerator: str) -> None:
    """Delegate format/device compatibility to the installed backend at startup."""
    if not path_value or not runtime or (not accelerator):
        return
    suffix = Path(path_value).suffix.lower()
    # Ultralytics adds export backends and device selectors independently of this
    # controller. Validate their availability when loading, without importing an
    # ML SDK or maintaining a restrictive hardware allowlist in configuration.
    if suffix not in _FAMILIAR_FORMATS:
        r.warn(
            "path",
            "export format and runtime dependencies will be checked by Ultralytics at startup",
        )


def _check_migration(r: Reader) -> None:
    """
    Explain the device -> runtime + accelerator split.

    reject_unknown would report 'device' as an unrecognised key and say nothing
    about what replaced it, which is a poor way to meet a rename.
    """
    old = r.raw_value("device")
    if old is None:
        return
    mapped = _DEVICE_MIGRATION.get(old if isinstance(old, str) else "")
    if mapped:
        runtime, accelerator = mapped
        hint = f"replace 'device: {old}' with 'runtime: {runtime}' and 'accelerator: {accelerator}'"
    else:
        hint = (
            "replace it with a runtime ("
            + " | ".join(RUNTIMES)
            + ") and an accelerator ("
            + " | ".join(ACCELERATORS)
            + ")"
        )
    r.error(
        r.path_of("device"),
        f"'device' was one key doing two jobs and has been split - {hint}",
    )


def _parse_one(r: Reader) -> ModelConfig:
    r.reject_unknown(*_MODEL_KEYS)
    model_id = r.identifier(
        "id",
        ID_PATTERN,
        "letters, digits, hyphens, and underscores only (cameras reference this id)",
    )
    name = r.string("name")
    version = r.string("version")
    path_value = r.string("path")
    _check_migration(r)
    runtime = r.enum("runtime", RUNTIMES)
    accelerator = r.string("accelerator")
    classes = r.string_list("classes")
    confidence_threshold = r.number("confidence_threshold", minimum=0.0, maximum=1.0)
    iou_threshold = r.number("iou_threshold", minimum=0.0, maximum=1.0)
    before_size = r.error_count
    input_size = r.int_pair("input_size", minimum=1)
    input_size_ok = r.error_count == before_size
    use_tracker = r.boolean("use_tracker")
    tracker = r.string("tracker")
    before_half = r.error_count
    half = r.boolean("half")
    half_ok = r.error_count == before_half
    ds_infer_config = r.optional_string("ds_infer_config")
    if ds_infer_config:
        r.error(
            r.path_of("ds_infer_config"), "DeepStream is outside this project scope"
        )
    _check_format(r, path_value, runtime, accelerator)
    if input_size_ok and any((v % _STRIDE for v in input_size)):
        r.warn(
            "input_size",
            f"{input_size} is not a multiple of {_STRIDE} - YOLO will resize internally",
        )
    if half_ok and half and path_value and accelerator:
        if accelerator in ("cpu", "mps", "coreml"):
            r.warn(
                "half",
                f"half has no effect on accelerator '{accelerator}' and will be ignored",
            )
        elif Path(path_value).suffix.lower() == ".engine":
            r.warn(
                "half", "half has no effect on a .engine - precision is fixed at export"
            )
    return ModelConfig(
        id=model_id,
        name=name,
        version=version,
        path=path_value,
        runtime=runtime,
        accelerator=accelerator,
        classes=classes,
        confidence_threshold=confidence_threshold,
        iou_threshold=iou_threshold,
        input_size=input_size,
        use_tracker=use_tracker,
        tracker=tracker,
        half=half,
        ds_infer_config=ds_infer_config,
    )


def parse_all(raw: Any) -> dict[str, ModelConfig]:
    """
    Parse and validate every model in models.yaml, keyed by id.

    File-existence checks for `path` and `tracker` are NOT done here. A model
    that no enabled camera uses is never loaded, so declaring more models than
    the device has weight files for is legitimate - the sample does exactly
    that. Those checks live in _validate, where cameras are in scope.
    """
    root = Reader(_FILE, {})
    if not isinstance(raw, list):
        root.error("models", f"expected a list of models, got {describe(raw)}")
        root.raise_if_errors()
    if not raw:
        root.error("models", "at least one model must be defined")
        root.raise_if_errors()
    models: dict[str, ModelConfig] = {}
    for i, item in enumerate(raw):
        path = f"models[{i}]"
        model = _parse_one(root.child(path, item))
        if model.id:
            if model.id in models:
                root.error(f"{path}.id", f"duplicate model id '{model.id}'")
            else:
                models[model.id] = model
    root.raise_if_errors()
    return models
