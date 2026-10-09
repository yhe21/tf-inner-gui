"""Runner regressions using synthetic images, never real-camera evidence."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from frame_capture import CameraSettings, FrameRecord, FrameRingBuffer


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "tools" / "test_frame_capture.py"
_SPEC = importlib.util.spec_from_file_location("timestamp_capture_test_runner", _RUNNER_PATH)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class SyntheticHistory:
    """Static generated frames for checking the runner's validation decisions."""

    def __init__(self, exposure_us=66657):
        self.settings = CameraSettings(66657, 4.87619, (3.04152, 1.50356))
        self.buffer = FrameRingBuffer(capacity=4)
        self.interval_ns = 66_666_667
        for sequence in range(24):
            timestamp = (sequence + 1) * self.interval_ns
            self.buffer.append(FrameRecord(
                sequence, timestamp, np.zeros((2, 3, 3), dtype=np.uint8),
                {"SensorTimestamp": timestamp, "ExposureTime": exposure_us,
                 "AnalogueGain": self.settings.analogue_gain,
                 "ColourGains": self.settings.colour_gains},
            ))
        self.signal_ns = self.buffer.snapshot()[-1].sensor_timestamp_ns - self.interval_ns // 2

    def stats(self):
        return {**self.buffer.stats(), "state": "running", "error": None}

    def snapshot(self):
        return self.buffer.snapshot()

    def get_before(self, timestamp_ns, timeout=1, max_age_ns=None):
        return self.buffer.get_before(timestamp_ns, max_age_ns=max_age_ns)


def synthetic_args():
    return SimpleNamespace(capacity=4, fill_timeout=1, query_timeout=0.1,
                           signals=2, fps=15, save_dir=None)


class HardwareRunnerValidationTests(unittest.TestCase):
    def test_cli_defaults_preserve_native_resolution_and_automatic_memory_budget(self):
        settings = CameraSettings(66657, 4.87619, (3.04152, 1.50356))
        camera = MagicMock()
        camera.__enter__.return_value = camera
        camera.stats.return_value = {"state": "stopped", "error": None}
        successful_checks = {"status": "passed", "hardware_test": True, "checks": {}}
        with patch.object(runner.CameraSettings, "from_json", return_value=settings), \
                patch.object(runner, "Picamera2FrameCapture", return_value=camera) as factory, \
                patch.object(runner, "self_test", return_value=successful_checks), \
                patch.object(runner, "emit"):
            exit_code = runner.main(["--self-test"])
        self.assertEqual(exit_code, 0)
        config = factory.call_args.args[0]
        self.assertIsNone(config.width)
        self.assertIsNone(config.height)
        self.assertIsNone(config.memory_budget_mb)
        self.assertEqual(config.capacity, 200)

    def test_numeric_but_clipped_exposure_cannot_pass_self_test(self):
        camera = SyntheticHistory(exposure_us=33333)
        with patch.object(runner, "now_sensor_clock_ns", return_value=camera.signal_ns), \
                patch.object(runner.time, "sleep"), patch.object(runner, "emit"):
            with self.assertRaisesRegex(AssertionError, "exposure_matches_setting"):
                runner.self_test(camera, synthetic_args())

    def test_consistent_synthetic_history_exercises_successful_validation(self):
        camera = SyntheticHistory()
        with patch.object(runner, "now_sensor_clock_ns", return_value=camera.signal_ns), \
                patch.object(runner.time, "sleep"), patch.object(runner, "emit"):
            report = runner.self_test(camera, synthetic_args())
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["checks"]["exposure_matches_setting"])
        self.assertTrue(report["checks"]["requested_fps_observed"])
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(report["observed_exposure_us_range"], [66657, 66657])
        self.assertEqual(len(report["matches"]), 2)
        self.assertTrue(all(match["sensor_timestamp_ns"] < match["signal_timestamp_ns"]
                            for match in report["matches"]))

    def test_late_acquisition_failure_replaces_success_report_and_returns_one(self):
        class LateFailureCamera:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                # The runner has finished its checks; acquisition fails while
                # the context is stopping. No cleanup exception is required.
                self.final_stats = {"state": "failed", "error": "late frame acquisition failure"}

            def stats(self):
                return self.final_stats

        camera = LateFailureCamera()
        successful_checks = {"status": "passed", "hardware_test": True, "checks": {}}
        settings = CameraSettings(66657, 4.87619, (3.04152, 1.50356))
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "synthetic_runner_report.json"
            with patch.object(runner.CameraSettings, "from_json", return_value=settings), \
                    patch.object(runner, "Picamera2FrameCapture", return_value=camera), \
                    patch.object(runner, "self_test", return_value=successful_checks), \
                    patch.object(runner, "emit") as emit:
                exit_code = runner.main(["--self-test", "--report", str(report_path)])
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertIn("late frame acquisition failure", report["error"])
        self.assertNotIn("clean_shutdown", report.get("checks", {}))
        events = [call.args[0] for call in emit.call_args_list]
        self.assertTrue(any(event["event"] == "failure" for event in events))
        self.assertFalse(any(event["event"] == "self_test_result" for event in events))


if __name__ == "__main__":
    unittest.main()
