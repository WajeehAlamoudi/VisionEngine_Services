"""BoxMOT adapter; no detector, camera capture or network dependencies."""

import importlib
import inspect
import logging
from pathlib import Path
import re
import time
import uuid

import numpy as np

from config.controller.parsers.tracker_algorithms import BOXMOT_ALGORITHMS, needs_frame

log = logging.getLogger(__name__)


class IdentityMotion:
    def apply(self, *_args, **_kwargs):
        return np.eye(2, 3, dtype=np.float32)


def prune_history(tracker):
    """Keep only removals still needed to evict a lost ByteTrack entry next tick."""
    lost = {track.id for track in tracker.lost_stracks}
    tracker.removed_stracks = [t for t in tracker.removed_stracks if t.id in lost]


class BoxMotTracker:
    def __init__(self, configuration, model, *, factory=None):
        self.config, self.model = configuration, model
        self.spec = BOXMOT_ALGORITHMS[configuration.algorithm]
        self.requires_frame = needs_frame(configuration.algorithm, configuration.params)
        self.classes = list(model.classes)
        self.class_indices = {name: i for i, name in enumerate(self.classes)}
        self.factory = factory
        self.reid = None
        self.trackers = {}
        self.reset()

    def reset(self):
        self.trackers.clear()
        self.session = uuid.uuid4()

    def _reid(self):
        if self.reid is not None:
            return self.reid
        import torch

        settings = self.config.reid
        weights = Path(settings["weights"]).resolve()
        if not weights.is_file():
            raise ValueError("ReID weights must exist locally before starting tracking")
        requested = settings["device"]
        if requested == "auto":
            accelerator = self.model.accelerator
            if accelerator in ("cpu", "cuda", "mps") or re.fullmatch(
                r"cuda:\d+", accelerator
            ):
                requested = accelerator
            elif accelerator.isdigit():
                requested = "cuda:" + accelerator
            else:
                requested = "cpu"
        device = torch.device(requested)
        if settings["backend"] == "pytorch":
            from boxmot.reid.backends.pytorch_backend import PyTorchBackend

            backend = PyTorchBackend
        else:
            from boxmot.reid.backends import tensorrt_backend

            # JetPack supplies TensorRT. Never let BoxMOT replace vendor packages.
            tensorrt_backend.ensure_reid_backend_requirements = lambda *_a, **_k: ()

            class SingleLoadBackend(tensorrt_backend.TensorRTBackend):
                def __init__(self, *args, **kwargs):
                    self.skip_base_load = True
                    super().__init__(*args, **kwargs)

                def load_model(self, weights):
                    if self.skip_base_load:
                        self.skip_base_load = False
                        return
                    # Published 19.0.0 checks dependencies through this instance.
                    if hasattr(self, "checker"):
                        self.checker.check_packages = lambda *_a, **_k: None
                    return super().load_model(weights)

            backend = SingleLoadBackend
        self.reid = backend(
            weights, device, half=settings["half"] and device.type == "cuda"
        )
        return self.reid

    def _create(self):
        if self.factory:
            return self.factory()
        # Registry describes the newer layout; published 19.0.0 uses flat packages.
        module, name = self.spec.class_path.rsplit(".", 1)
        try:
            tracker_class = getattr(importlib.import_module(module), name)
        except ModuleNotFoundError as exc:
            if not exc.name or not (
                module == exc.name or module.startswith(exc.name + ".")
            ):
                raise
            module = module.replace(".bbox.", ".")
            module += "." + self.config.algorithm
            tracker_class = getattr(importlib.import_module(module), name)
        sdk_log = logging.getLogger("boxmot")
        sdk_log.handlers.clear()
        sdk_log.propagate = True
        sdk_log.setLevel(logging.NOTSET)
        params = dict(self.config.params)
        # Separate instances also isolate lost/history state when tracking per class.
        params["per_class"] = False
        use_cmc = params.pop("use_cmc", True)
        if self.config.algorithm in ("boosttrack", "occluboost"):
            params["use_ecc"] = use_cmc
        accepted = set()
        for parent in tracker_class.__mro__:
            accepted.update(inspect.signature(parent.__init__).parameters)
        for key in set(params) - accepted:
            if params[key] != self.spec.defaults.get(key):
                raise ValueError(
                    f"installed BoxMOT does not support configured parameter '{key}'"
                )
            params.pop(key)
        if self.spec.needs_reid(self.config.params):
            params["reid_model"] = self._reid()
        tracker = tracker_class(**params)
        if self.config.algorithm == "botsort" and not use_cmc:
            tracker.cmc = IdentityMotion()
        return tracker

    def load(self):
        if not self.config.params.get("per_class"):
            self.trackers[None] = self._create()
        else:
            # Finite, configured class set. No unbounded UUID or class history maps.
            self.trackers = {i: self._create() for i in range(len(self.classes))}

    def update(self, batch, image=None):
        started = time.perf_counter()
        width, height = batch["width"], batch["height"]
        if width * height * 3 > 64 * 1024 * 1024:
            raise ValueError("frame dimensions exceed the frame contract")
        if self.requires_frame:
            if image is None or image.shape != (height, width, 3):
                raise ValueError(
                    "selected tracker requires the matching original frame"
                )
        else:
            # These algorithms read dimensions only. This view allocates three bytes.
            image = np.broadcast_to(
                np.zeros((1, 1, 3), dtype=np.uint8), (height, width, 3)
            )
        rows = []
        for detection in batch["detections"]:
            name = detection["class_name"]
            if name not in self.class_indices:
                raise ValueError("received a class outside the configured model")
            rows.append(
                [*detection["bbox"], detection["confidence"], self.class_indices[name]]
            )
        detections = np.asarray(rows, dtype=np.float32).reshape(-1, 6)
        results = []
        for category, tracker in self.trackers.items():
            selected = (
                detections
                if category is None
                else detections[detections[:, 5] == category]
            )
            # Empty batches advance track lifecycle too.
            tracks = tracker.update(selected, image)
            if self.config.algorithm == "bytetrack":
                prune_history(tracker)
            tracks = np.asarray(tracks)
            if tracks.size and (tracks.ndim != 2 or tracks.shape[1] != 8):
                raise ValueError("tracker returned an unsupported result layout")
            for row in tracks.reshape(-1, 8):
                if not np.isfinite(row).all():
                    raise ValueError("tracker returned non-finite results")
                class_id = int(row[6])
                if not 0 <= class_id < len(self.classes):
                    raise ValueError("tracker returned an unknown class")
                box = np.clip(row[:4], [0, 0, 0, 0], [width, height, width, height])
                if box[0] > box[2] or box[1] > box[3]:
                    raise ValueError("tracker returned an inverted bounding box")
                results.append(
                    {
                        "class_name": self.classes[class_id],
                        "confidence": float(np.clip(row[5], 0, 1)),
                        "bbox": box.tolist(),
                        "track_id": str(
                            uuid.uuid5(self.session, f"{category}:{int(row[4])}")
                        ),
                    }
                )
        return {
            **batch,
            "detections": results,
            "tracking": {
                "algorithm": self.config.algorithm,
                "session_id": str(self.session),
                "tracked_at_ns": time.time_ns(),
                "tracking_ms": (time.perf_counter() - started) * 1000,
            },
        }
