"""Fixed-ROI INNER and GLUE classification for Raspberry Pi inference."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_ROOT = APP_DIR.parent / "models"
PADDING_RGB = (114, 114, 114)

# Coordinates are measured on the 3040x4056 image after the camera frame is
# rotated 90 degrees counterclockwise.
INSPECTION_CONFIG = {
    "INNER": {
        "model_dir": "inner_cls_ncnn_model",
        "imgsz": 320,
        "left": (1428, 2444, 216, 316),
        "right": (2196, 2436, 216, 308),
    },
    "GLUE": {
        "model_dir": "glue_cls_ncnn_model",
        "imgsz": 224,
        "left": (1436, 2456, 208, 88),
        "right": (2188, 2448, 212, 96),
    },
}


@dataclass(frozen=True)
class SidePrediction:
    label: str
    confidence: float


@dataclass(frozen=True)
class InspectionResult:
    command: str
    overall_label: str
    left: SidePrediction
    right: SidePrediction
    elapsed_ms: float

    @property
    def confidence(self) -> float:
        """Return the conservative confidence shown for the overall result."""
        return min(self.left.confidence, self.right.confidence)


def crop_and_pad(image: object, roi: Tuple[int, int, int, int], size: int) -> object:
    """Crop one fixed ROI and center it on the training-time gray square."""
    from PIL import Image

    x, y, width, height = roi
    image_width, image_height = image.size
    if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
        raise ValueError(
            f"ROI {roi} is outside image size {image_width}x{image_height}"
        )
    cropped = image.crop((x, y, x + width, y + height)).convert("RGB")
    padded = Image.new("RGB", (size, size), PADDING_RGB)
    padded.paste(cropped, ((size - width) // 2, (size - height) // 2))
    return padded


class FixedRoiClassifier:
    """Load both NCNN models once and classify the left/right fixed ROIs."""

    def __init__(
        self,
        model_root: Path = DEFAULT_MODEL_ROOT,
        model_factory: Optional[Callable[[Path], object]] = None,
        warmup: bool = True,
    ) -> None:
        self.model_root = Path(model_root)
        if model_factory is None:
            from ultralytics import YOLO

            model_factory = lambda path: YOLO(str(path), task="classify")

        self.models: Dict[str, object] = {}
        for command, config in INSPECTION_CONFIG.items():
            model_path = self.model_root / str(config["model_dir"])
            if not model_path.is_dir():
                raise FileNotFoundError(f"{command} model not found: {model_path}")
            self.models[command] = model_factory(model_path)

        if warmup:
            self.warmup()

    def warmup(self) -> None:
        """Force both lazy NCNN backends to load before the first trigger."""
        from PIL import Image

        for command, model in self.models.items():
            size = int(INSPECTION_CONFIG[command]["imgsz"])
            dummy = Image.new("RGB", (size, size), PADDING_RGB)
            model.predict(source=dummy, imgsz=size, verbose=False)

    def inspect(self, command: str, rotated_image: object) -> InspectionResult:
        command = command.upper()
        if command not in INSPECTION_CONFIG:
            raise ValueError(f"Unsupported inspection command: {command}")

        started_at = time.perf_counter()
        config = INSPECTION_CONFIG[command]
        size = int(config["imgsz"])
        crops = [
            crop_and_pad(rotated_image, config["left"], size),
            crop_and_pad(rotated_image, config["right"], size),
        ]
        results = self.models[command].predict(
            source=crops,
            imgsz=size,
            verbose=False,
        )
        if len(results) != 2:
            raise RuntimeError(
                f"{command} model returned {len(results)} results instead of 2"
            )

        predictions = []
        for result in results:
            if result.probs is None:
                raise RuntimeError(f"{command} model returned no class probabilities")
            top1 = int(result.probs.top1)
            predictions.append(
                SidePrediction(
                    label=str(result.names[top1]).upper(),
                    confidence=float(result.probs.top1conf),
                )
            )

        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        left, right = predictions
        overall_label = "OK" if left.label == "OK" and right.label == "OK" else "NG"
        return InspectionResult(
            command=command,
            overall_label=overall_label,
            left=left,
            right=right,
            elapsed_ms=elapsed_ms,
        )
