"""Regression tests for runtime ROI translation in rotated-camera coordinates."""

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from inspection import (  # noqa: E402
    FixedRoiClassifier,
    INSPECTION_CONFIG,
    PADDING_RGB,
    SidePrediction,
)
from roi_offsets import (  # noqa: E402
    DEFAULT_ROI_OFFSETS_PATH,
    RoiOffsets,
    load_roi_offsets,
)


class RoiOffsetConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.default_path = self.root / "bundled.json"
        self.override_path = self.root / "device.json"
        self.write_config(self.default_path, 0, -32)

    @staticmethod
    def write_config(path, x, y):
        path.write_text(json.dumps({
            "schema_version": 1,
            "offset_x_px": x,
            "offset_y_px": y,
        }), encoding="utf-8")

    def load(self):
        return load_roi_offsets(
            default_path=self.default_path, override_path=self.override_path
        )

    def test_bundled_configuration_moves_up_32_pixels(self):
        offsets = load_roi_offsets(
            default_path=DEFAULT_ROI_OFFSETS_PATH,
            override_path=self.override_path,
        )
        self.assertEqual(offsets, RoiOffsets(0, -32))

    def test_absent_override_uses_bundled_configuration(self):
        self.assertEqual(self.load(), RoiOffsets(0, -32))

    def test_existing_device_override_replaces_bundled_configuration(self):
        self.write_config(self.override_path, 7, 18)
        self.assertEqual(self.load(), RoiOffsets(7, 18))

    def test_valid_override_does_not_require_bundled_file(self):
        self.write_config(self.override_path, -9, 0)
        self.default_path.unlink()
        self.assertEqual(self.load(), RoiOffsets(-9, 0))

    def test_incomplete_override_does_not_merge_or_fall_back(self):
        self.override_path.write_text(
            '{"schema_version": 1, "offset_x_px": 12}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_json_override_does_not_fall_back(self):
        self.override_path.write_text('{"schema_version":', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_configuration_shapes_fail_clearly(self):
        for payload in ([], None, "offsets", 32):
            with self.subTest(payload=payload):
                self.override_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load()

    def test_schema_version_must_be_supported_integer(self):
        for version in (0, 2, True, 1.0, "1", None):
            with self.subTest(version=version):
                self.override_path.write_text(json.dumps({
                    "schema_version": version,
                    "offset_x_px": 0,
                    "offset_y_px": -32,
                }), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load()

    def test_each_required_field_must_be_present(self):
        complete = {"schema_version": 1, "offset_x_px": 0, "offset_y_px": -32}
        for field in complete:
            with self.subTest(field=field):
                payload = dict(complete)
                del payload[field]
                self.override_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.load()

    def test_offsets_reject_boolean_float_string_and_null(self):
        for field in ("offset_x_px", "offset_y_px"):
            for value in (True, False, 1.0, -32.5, "-32", None, [0]):
                with self.subTest(field=field, value=value):
                    payload = {"schema_version": 1, "offset_x_px": 0, "offset_y_px": -32}
                    payload[field] = value
                    self.override_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.load()

    def test_missing_both_files_fails_clearly(self):
        self.default_path.unlink()
        with self.assertRaises(OSError):
            self.load()

    def test_unreadable_override_does_not_fall_back(self):
        self.write_config(self.override_path, 0, -32)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                self.load()

    def test_zero_positive_and_negative_offsets_preserve_width_and_height(self):
        source_roi = (100, 200, 216, 316)
        for x, y in ((0, 0), (12, 32), (-12, -32), (-12, 32), (12, -32)):
            with self.subTest(x=x, y=y):
                self.assertEqual(
                    RoiOffsets(x, y).apply(source_roi),
                    (100 + x, 200 + y, 216, 316),
                )
        self.assertEqual(source_roi, (100, 200, 216, 316))

    def test_direct_offsets_reject_non_integer_values(self):
        for value in (True, False, 1.0, -32.5, "-32", None):
            for field in ("offset_x_px", "offset_y_px"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        RoiOffsets(**{field: value})

    def test_offsets_are_immutable(self):
        offsets = RoiOffsets(0, -32)
        with self.assertRaises(FrozenInstanceError):
            offsets.offset_y_px = 0


class RecordingModel:
    def __init__(self):
        self.sources = []

    def predict(self, image):
        self.sources.append(image.copy())
        return SidePrediction("OK", 0.99)


class RoiOffsetClassifierTests(unittest.TestCase):
    BASELINE_ROIS = {
        "INNER": ((1428, 2444, 216, 316), (2196, 2436, 216, 308)),
        "GLUE": ((1436, 2456, 208, 88), (2188, 2448, 212, 96)),
    }

    @classmethod
    def setUpClass(cls):
        # Channels encode x and y independently, so a reversed axis/sign or a
        # translation before rotation cannot accidentally match the right crop.
        width, height = 3040, 4056
        red = Image.frombytes("L", (width, height),
                              bytes(x % 251 for x in range(width)) * height)
        green = Image.frombytes("L", (width, height), b"".join(
            bytes([y % 251]) * width for y in range(height)
        ))
        blue = Image.new("L", (width, height), 71)
        cls.source_image = Image.merge("RGB", (red, green, blue))

    @classmethod
    def tearDownClass(cls):
        cls.source_image.close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.model_root = Path(self.temporary.name)
        for config in INSPECTION_CONFIG.values():
            (self.model_root / config["model_dir"]).mkdir()

    def make_engine(self, offsets):
        models = {command: RecordingModel() for command in INSPECTION_CONFIG}
        names = {config["model_dir"]: command
                 for command, config in INSPECTION_CONFIG.items()}
        engine = FixedRoiClassifier(
            self.model_root,
            model_factory=lambda path: models[names[path.name]],
            warmup=False,
            roi_offsets=offsets,
        )
        return engine, models

    def assert_crops_match(self, command, sources, dx, dy):
        size = 320 if command == "INNER" else 224
        self.assertEqual(len(sources), 2)
        for source, (x, y, width, height) in zip(sources, self.BASELINE_ROIS[command]):
            with self.subTest(command=command, x=x, y=y):
                self.assertEqual(source.size, (size, size))
                expected = Image.new("RGB", (size, size), PADDING_RGB)
                expected.paste(self.source_image.crop((
                    x + dx, y + dy, x + dx + width, y + dy + height,
                )), ((size - width) // 2, (size - height) // 2))
                self.assertIsNone(ImageChops.difference(source, expected).getbbox())
                self.assertEqual(source.getpixel((0, 0)), PADDING_RGB)
                self.assertEqual(source.getpixel((size - 1, size - 1)), PADDING_RGB)

    def test_all_four_rois_sample_32_pixels_up_and_keep_padding_and_source(self):
        original = hashlib.sha256(self.source_image.tobytes()).digest()
        engine, models = self.make_engine(RoiOffsets(0, -32))
        for command in ("INNER", "GLUE"):
            self.assertEqual(engine.inspect(command, self.source_image).overall_label, "OK")
            self.assert_crops_match(command, models[command].sources, 0, -32)
        self.assertEqual(hashlib.sha256(self.source_image.tobytes()).digest(), original)
        self.assertEqual(self.source_image.size, (3040, 4056))

    def test_zero_offset_reproduces_historical_crop_coordinates(self):
        engine, models = self.make_engine(RoiOffsets(0, 0))
        for command in ("INNER", "GLUE"):
            engine.inspect(command, self.source_image)
            self.assert_crops_match(command, models[command].sources, 0, 0)

    def test_positive_and_negative_xy_offsets_sample_correct_pixels(self):
        for dx, dy in ((17, 23), (-17, -23), (17, -23), (-17, 23)):
            with self.subTest(dx=dx, dy=dy):
                engine, models = self.make_engine(RoiOffsets(dx, dy))
                for command in ("INNER", "GLUE"):
                    engine.inspect(command, self.source_image)
                    self.assert_crops_match(command, models[command].sources, dx, dy)

    def test_invalid_offset_fails_before_any_model_prediction(self):
        for offsets in (RoiOffsets(-1500, 0), RoiOffsets(1000, 0),
                        RoiOffsets(0, -2500), RoiOffsets(0, 1700)):
            for command in ("INNER", "GLUE"):
                with self.subTest(offsets=offsets, command=command):
                    engine, models = self.make_engine(offsets)
                    with self.assertRaisesRegex(ValueError, "outside image"):
                        engine.inspect(command, self.source_image)
                    self.assertTrue(all(not model.sources for model in models.values()))

    def test_exact_image_edge_is_valid_without_clamping(self):
        # INNER left x becomes zero; INNER right remains inside the image.
        engine, models = self.make_engine(RoiOffsets(-1428, -2436))
        engine.inspect("INNER", self.source_image)
        self.assert_crops_match("INNER", models["INNER"].sources, -1428, -2436)

    def test_multiple_instances_do_not_modify_or_accumulate_base_coordinates(self):
        original = copy.deepcopy(INSPECTION_CONFIG)
        for offsets in (RoiOffsets(0, -32), RoiOffsets(0, -32), RoiOffsets()):
            engine, models = self.make_engine(offsets)
            for command in ("INNER", "GLUE"):
                engine.inspect(command, self.source_image)
                self.assert_crops_match(command, models[command].sources,
                                        offsets.offset_x_px, offsets.offset_y_px)
        self.assertEqual(INSPECTION_CONFIG, original)
        for command, (left, right) in self.BASELINE_ROIS.items():
            self.assertEqual(INSPECTION_CONFIG[command]["left"], left)
            self.assertEqual(INSPECTION_CONFIG[command]["right"], right)

    def test_default_configuration_loads_once_not_on_each_capture(self):
        with mock.patch("inspection.load_roi_offsets", return_value=RoiOffsets(0, -32)) as load:
            engine, models = self.make_engine(None)
            load.assert_called_once_with()
            for command in ("INNER", "GLUE", "INNER", "GLUE"):
                engine.inspect(command, self.source_image)
            load.assert_called_once_with()
            self.assertEqual(len(models["INNER"].sources), 4)
            self.assertEqual(len(models["GLUE"].sources), 4)

    def test_explicit_offsets_do_not_read_device_configuration(self):
        with mock.patch("inspection.load_roi_offsets", side_effect=AssertionError("unexpected I/O")):
            engine, models = self.make_engine(RoiOffsets())
            engine.inspect("GLUE", self.source_image)
        self.assert_crops_match("GLUE", models["GLUE"].sources, 0, 0)

    def test_configuration_change_applies_on_new_classifier_only(self):
        config_path = self.model_root / "device.json"
        unused_default = self.model_root / "absent-default.json"
        def write_offset(y):
            config_path.write_text(json.dumps({
                "schema_version": 1, "offset_x_px": 0, "offset_y_px": y,
            }), encoding="utf-8")
        def load_device():
            return load_roi_offsets(default_path=unused_default, override_path=config_path)

        write_offset(-32)
        with mock.patch("inspection.load_roi_offsets", side_effect=load_device) as load:
            first_engine, first_models = self.make_engine(None)
            write_offset(-48)
            first_engine.inspect("GLUE", self.source_image)
            self.assert_crops_match("GLUE", first_models["GLUE"].sources, 0, -32)
            second_engine, second_models = self.make_engine(None)
            second_engine.inspect("GLUE", self.source_image)
            self.assert_crops_match("GLUE", second_models["GLUE"].sources, 0, -48)
            self.assertEqual(load.call_count, 2)


if __name__ == "__main__":
    unittest.main()
