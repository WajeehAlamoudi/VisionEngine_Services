"""One YOLO instance, used serially by one inference worker."""

from pathlib import Path
import time


def _precision_options(config, defaults):
    if config.accelerator == "coreml":
        return {}
    use_half = config.half and config.accelerator not in ("cpu", "mps", "mps:0")
    if "quantize" in defaults:
        return {"quantize": 16 if use_half else 32}
    return {"half": use_half}


class YoloModel:
    def __init__(self, config):
        self.config = config
        self.model = None

    def load(self):
        path = Path(self.config.path)
        if not path.is_file() and not path.is_dir():
            raise ValueError(
                "configured model asset is missing; automatic downloads are disabled"
            )
        from ultralytics import YOLO
        from ultralytics.cfg import DEFAULT_CFG_DICT

        self.device = self.config.accelerator
        self.model = YOLO(str(path), task="detect")
        self.precision = _precision_options(self.config, DEFAULT_CFG_DICT)
        if "quantize" in DEFAULT_CFG_DICT:
            self.model.overrides.pop("half", None)
        self.model.overrides.update(self.precision)
        # Exported backends can initialize while reading names, before predict.
        # Apply deployment options before that first backend initialization.
        if self.device not in ("auto", "coreml"):
            self.model.overrides["device"] = self.device
        if self.model.task != "detect":
            raise ValueError("this service supports object detection models only")
        self.names = dict(self.model.names)
        self.indices = {name: index for index, name in self.names.items()}
        unknown = set(self.config.classes) - self.indices.keys()
        if unknown:
            raise ValueError("configured model classes do not match the loaded model")

    def infer(self, image, active_classes):
        selected = active_classes or self.config.classes
        indices = [self.indices[name] for name in selected]
        width, height = self.config.input_size
        options = dict(
            conf=self.config.confidence_threshold,
            iou=self.config.iou_threshold,
            imgsz=(height, width),
            classes=indices,
            verbose=False,
            save=False,
        )
        if self.device not in ("auto", "coreml"):
            options["device"] = self.device
        options.update(self.precision)
        started = time.monotonic()
        results = self.model.predict(image, **options)
        detections = []
        for result in results:
            if result.boxes is None:
                continue
            for xyxy, confidence, category in zip(
                result.boxes.xyxy.cpu().tolist(),
                result.boxes.conf.cpu().tolist(),
                result.boxes.cls.cpu().tolist(),
            ):
                box = [
                    max(
                        0.0,
                        min(
                            float(value),
                            image.shape[1] if index % 2 == 0 else image.shape[0],
                        ),
                    )
                    for index, value in enumerate(xyxy)
                ]
                detections.append(
                    {
                        "class_name": self.names[int(category)],
                        "confidence": float(confidence),
                        "bbox": box,
                        "track_id": None,
                    }
                )
        return detections, (time.monotonic() - started) * 1000

    def close(self):
        self.model = None
