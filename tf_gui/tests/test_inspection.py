import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from inspection import (  # noqa: E402
    FixedRoiClassifier,
    INSPECTION_CONFIG,
    SidePrediction,
)


class FakeModel:
    def __init__(self, labels):
        self.labels = labels
        self.sources = []

    def predict(self, source):
        self.sources.append(source)
        label, confidence = self.labels[len(self.sources) - 1]
        return SidePrediction(label, confidence)


class InspectionTests(unittest.TestCase):
    def test_both_sides_must_be_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for config in INSPECTION_CONFIG.values():
                (root / config["model_dir"]).mkdir()

            models = {
                "inner_cls_ncnn_model": FakeModel(
                    [("OK", 0.98), ("NG", 0.91)]
                ),
                "glue_cls_ncnn_model": FakeModel(
                    [("OK", 0.99), ("OK", 0.97)]
                ),
            }
            engine = FixedRoiClassifier(
                root,
                model_factory=lambda path: models[path.name],
                warmup=False,
            )

            with mock.patch(
                "inspection.crop_and_pad", side_effect=["left", "right"]
            ):
                result = engine.inspect("INNER", object())

            self.assertEqual(result.overall_label, "NG")
            self.assertEqual(result.left.label, "OK")
            self.assertEqual(result.right.label, "NG")
            self.assertAlmostEqual(result.confidence, 0.91)
            self.assertEqual(models["inner_cls_ncnn_model"].sources, ["left", "right"])

    def test_runtime_module_does_not_require_torch_or_ultralytics(self) -> None:
        source = (PROJECT_DIR / "inspection.py").read_text(encoding="utf-8")
        self.assertNotIn("import torch", source)
        self.assertNotIn("import ultralytics", source)

    def test_missing_model_directory_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(FileNotFoundError, "INNER model not found"):
                FixedRoiClassifier(
                    Path(temporary_directory),
                    model_factory=lambda _path: FakeModel([]),
                    warmup=False,
                )


if __name__ == "__main__":
    unittest.main()
