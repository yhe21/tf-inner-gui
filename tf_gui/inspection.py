"""Fixed-ROI INNER and GLUE classification using NCNN directly."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_ROOT = APP_DIR.parent / "models"
PADDING_RGB = (114, 114, 114)
CLASS_NAMES = ("NG", "OK")
OK_CONFIDENCE_THRESHOLD = 0.90
REVIEW_SAVE_OK_CONFIDENCE_THRESHOLD = 0.95

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
    was_bypassed: bool = False

    @property
    def confidence(self) -> float:
        """Return the conservative confidence shown for the overall result."""
        return min(self.left.confidence, self.right.confidence)

    @classmethod
    def forced_ok(cls, command: str) -> "InspectionResult":
        """Build an explicit OK result when inspection bypass is enabled."""
        return cls(
            command=command.upper(),
            overall_label="OK",
            left=SidePrediction("OK", 1.0),
            right=SidePrediction("OK", 1.0),
            elapsed_ms=0.0,
            was_bypassed=True,
        )


def requires_review_save(
    result: InspectionResult,
    ok_confidence_threshold: float = REVIEW_SAVE_OK_CONFIDENCE_THRESHOLD,
) -> bool:
    """Return whether a real AI result should be retained for review/training."""
    if result.was_bypassed:
        return False
    return any(
        prediction.label != "OK"
        or prediction.confidence < ok_confidence_threshold
        for prediction in (result.left, result.right)
    )


class NcnnClassificationModel:
    """Run an exported Ultralytics classification model without PyTorch.

    The NCNN export already contains the complete neural network, including
    its final softmax. Direct loading avoids importing Ultralytics/PyTorch on
    Raspberry Pi, where pip Torch wheels can conflict with the system BLAS.
    """

    def __init__(
        self,
        model_dir: Path,
        class_names: Sequence[str] = CLASS_NAMES,
        input_name: str = "in0",
        output_name: str = "out0",
        num_threads: int = 4,
    ) -> None:
        import ncnn

        self.ncnn = ncnn
        self.class_names = tuple(class_names)
        self.input_name = input_name
        self.output_name = output_name
        self.net = ncnn.Net()
        self.net.opt.num_threads = max(1, int(num_threads))
        self.net.opt.use_vulkan_compute = False

        parameter_file = Path(model_dir) / "model.ncnn.param"
        weights_file = Path(model_dir) / "model.ncnn.bin"
        for required_file in (parameter_file, weights_file):
            if not required_file.is_file():
                raise FileNotFoundError(f"NCNN model file not found: {required_file}")

        if self.net.load_param(str(parameter_file)) != 0:
            raise RuntimeError(f"Unable to load NCNN parameters: {parameter_file}")
        if self.net.load_model(str(weights_file)) != 0:
            raise RuntimeError(f"Unable to load NCNN weights: {weights_file}")

    def predict(self, image: object) -> SidePrediction:
        """Classify one RGB PIL image using training-time preprocessing."""
        import numpy as np

        rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"Expected an RGB image, got array shape {rgb.shape}")

        # Ultralytics classification inference uses RGB CHW float32 / 255.
        chw = np.ascontiguousarray(rgb.transpose(2, 0, 1) / 255.0)
        input_mat = self.ncnn.Mat(chw).clone()
        with self.net.create_extractor() as extractor:
            input_status = extractor.input(self.input_name, input_mat)
            if input_status != 0:
                raise RuntimeError(f"NCNN input failed with status {input_status}")
            output_status, output = extractor.extract(self.output_name)
            if output_status != 0:
                raise RuntimeError(f"NCNN inference failed with status {output_status}")

        probabilities = np.asarray(output, dtype=np.float32).reshape(-1)
        if len(probabilities) != len(self.class_names):
            raise RuntimeError(
                "NCNN returned "
                f"{len(probabilities)} classes, expected {len(self.class_names)}"
            )
        top1 = int(probabilities.argmax())
        return SidePrediction(
            label=self.class_names[top1],
            confidence=float(probabilities[top1]),
        )


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
        ok_confidence_threshold: float = OK_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.model_root = Path(model_root)
        self.ok_confidence_threshold = float(ok_confidence_threshold)
        if not 0.0 <= self.ok_confidence_threshold <= 1.0:
            raise ValueError("OK confidence threshold must be between 0 and 1")
        if model_factory is None:
            model_factory = NcnnClassificationModel

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
            model.predict(dummy)

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
        predictions = [self.models[command].predict(crop) for crop in crops]

        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        left, right = predictions
        overall_label = (
            "OK"
            if all(
                prediction.label == "OK"
                and prediction.confidence >= self.ok_confidence_threshold
                for prediction in predictions
            )
            else "NG"
        )
        return InspectionResult(
            command=command,
            overall_label=overall_label,
            left=left,
            right=right,
            elapsed_ms=elapsed_ms,
        )
