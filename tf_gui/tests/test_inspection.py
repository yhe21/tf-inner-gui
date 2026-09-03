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
    InspectionResult,
    SidePrediction,
    requires_review_save,
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

    def test_ok_requires_90_percent_confidence_on_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for config in INSPECTION_CONFIG.values():
                (root / config["model_dir"]).mkdir()

            inner_model = FakeModel([("OK", 0.90), ("OK", 0.99)])
            models = {
                "inner_cls_ncnn_model": inner_model,
                "glue_cls_ncnn_model": FakeModel([]),
            }
            engine = FixedRoiClassifier(
                root,
                model_factory=lambda path: models[path.name],
                warmup=False,
            )
            with mock.patch(
                "inspection.crop_and_pad", side_effect=["left", "right"]
            ):
                passing = engine.inspect("INNER", object())
            self.assertEqual(passing.overall_label, "OK")

            models["inner_cls_ncnn_model"] = FakeModel(
                [("OK", 0.8999), ("OK", 0.99)]
            )
            engine = FixedRoiClassifier(
                root,
                model_factory=lambda path: models[path.name],
                warmup=False,
            )
            with mock.patch(
                "inspection.crop_and_pad", side_effect=["left", "right"]
            ):
                failing = engine.inspect("INNER", object())
            self.assertEqual(failing.overall_label, "NG")

    def test_ng_always_fails_without_a_confidence_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for config in INSPECTION_CONFIG.values():
                (root / config["model_dir"]).mkdir()

            models = {
                "inner_cls_ncnn_model": FakeModel(
                    [("NG", 0.01), ("OK", 0.99)]
                ),
                "glue_cls_ncnn_model": FakeModel([]),
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

    def test_review_saving_uses_95_percent_ok_threshold(self) -> None:
        high_confidence_ok = InspectionResult(
            "INNER",
            "OK",
            SidePrediction("OK", 0.95),
            SidePrediction("OK", 0.99),
            10.0,
        )
        low_confidence_ok = InspectionResult(
            "INNER",
            "OK",
            SidePrediction("OK", 0.9499),
            SidePrediction("OK", 0.99),
            10.0,
        )
        any_ng = InspectionResult(
            "INNER",
            "NG",
            SidePrediction("NG", 0.51),
            SidePrediction("OK", 0.99),
            10.0,
        )

        self.assertFalse(requires_review_save(high_confidence_ok))
        self.assertTrue(requires_review_save(low_confidence_ok))
        self.assertTrue(requires_review_save(any_ng))
        self.assertFalse(requires_review_save(InspectionResult.forced_ok("INNER")))

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
