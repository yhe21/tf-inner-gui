"""Soak-runner regressions with simulated clocks; no real-camera evidence."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from frame_capture import CameraSettings, FrameRecord, FrameUnavailable


_PATH = Path(__file__).resolve().parents[2] / "tools" / "soak_frame_capture.py"
_SPEC = importlib.util.spec_from_file_location("timestamp_capture_soak_runner", _PATH)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class SimulatedClock:
    def __init__(self, interrupt_at=None):
        self.value = 100.0
        self.interrupt_at = interrupt_at
        self.sleep_calls = 0

    def monotonic(self):
        return self.value

    def signal(self):
        return round(self.value * 1_000_000_000)

    def sleep(self, seconds):
        self.sleep_calls += 1
        if self.sleep_calls > 100_000:
            raise RuntimeError("Simulation failed to advance; possible zero-delay busy loop")
        self.value += seconds
        if self.interrupt_at is not None and self.value >= self.interrupt_at:
            raise KeyboardInterrupt()


class SimulatedCamera:
    """Generate only tiny synthetic frames, retaining no images between calls."""

    def __init__(self, clock, *, first_query_timeout=False, fail_at=None,
                 late_failure=False, cleanup_failure=False, never_fill=False):
        self.clock = clock
        self.first_query_timeout = first_query_timeout
        self.fail_at = fail_at
        self.late_failure = late_failure
        self.cleanup_failure = cleanup_failure
        self.never_fill = never_fill
        self.state = "new"
        self.query_calls = 0
        self.stop_calls = 0
        self.error = None

    def start(self):
        self.state = "running"
        return self

    def stop(self, timeout=3):
        self.stop_calls += 1
        self.state = "stopped"
        if self.late_failure:
            self.state = "failed"
            self.error = "late acquisition failure"
        if self.cleanup_failure:
            raise FrameUnavailable("timeout", "simulated cleanup timeout")

    def stats(self):
        if self.fail_at is not None and self.clock.value >= self.fail_at:
            self.state = "failed"
            self.error = "simulated acquisition failure"
        total = 0 if self.never_fill else 30 + int((self.clock.value - 100) * 10)
        return {"capacity": 30, "count": min(30, total), "total_appended": total,
                "evicted": max(0, total - 30), "state": self.state,
                "error": self.error, "cleanup_error": None,
                "image_bytes": min(30, total) * 18,
                "oldest_timestamp_ns": self.clock.signal() - 2_900_000_000,
                "latest_timestamp_ns": self.clock.signal(),
                "duplicates_dropped": 0}

    def snapshot(self):
        latest = self.clock.signal()
        sequence = 30 + int((self.clock.value - 100) * 10)
        return tuple(FrameRecord(
            sequence - offset, latest - offset * 100_000_000,
            np.zeros((2, 3, 3), dtype=np.uint8),
            {"SensorTimestamp": latest - offset * 100_000_000,
             "ExposureTime": 66654, "AnalogueGain": 4.87619,
             "ColourGains": (3.04152, 1.50356)},
        ) for offset in range(29, -1, -1))

    def get_before(self, timestamp_ns, timeout=1, max_age_ns=None):
        self.query_calls += 1
        if self.first_query_timeout and self.query_calls == 1:
            self.clock.value += min(timeout, 0.1)
            raise FrameUnavailable("timeout", "simulated signal timeout")
        if self.stats()["error"]:
            raise FrameUnavailable("capture_failed", self.error)
        return max((record for record in self.snapshot()
                    if record.sensor_timestamp_ns < timestamp_ns),
                   key=lambda record: record.sensor_timestamp_ns)


def telemetry(value=0, temperature=50):
    return {"temperature_c": temperature,
            "temperature_error": None if temperature is not None else "sensor unavailable",
            "rss_bytes": 1024 * 1024, "rss_peak_bytes": 2 * 1024 * 1024,
            "rss_error": None,
            "throttled_raw": None if value is None else hex(value),
            "throttled_value": value,
            "throttled_current": None if value is None else {},
            "throttled_history": None if value is None else {},
            "throttled_error": "vcgencmd unavailable" if value is None else None}


class SoakRunnerTests(unittest.TestCase):
    def run_simulation(self, *, clock=None, camera=None, probe=None, **overrides):
        clock = clock or SimulatedClock()
        camera = camera or SimulatedCamera(clock)
        args = runner.build_parser().parse_args([])
        for key, value in {"duration": 4, "sample_interval": 1,
                           "query_interval": 0.5, **overrides}.items():
            setattr(args, key, value)
        events = []
        report = runner.run_soak(camera, args, events.append,
                                 probe=probe or (lambda: telemetry()),
                                 monotonic=clock.monotonic, sleep=clock.sleep,
                                 signal_clock=clock.signal)
        return report, events, camera

    def test_ten_minutes_can_be_exercised_without_wall_clock_waits(self):
        report, events, camera = self.run_simulation(duration=600, sample_interval=5)
        self.assertEqual(report["status"], "passed")
        self.assertGreaterEqual(report["summary"]["elapsed_seconds"], 600)
        self.assertGreaterEqual(len(report["samples"]), 120)
        self.assertGreater(report["queries"]["succeeded"], 0)
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(camera.stop_calls, 1)
        self.assertEqual(events[-1]["event"], "finished")
        # Serialization also catches leaked FrameRecord / numpy image objects.
        json.dumps({key: value for key, value in report.items() if key != "configuration"})

    def test_query_timeout_continues_sampling_but_cannot_pass(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, first_query_timeout=True)
        report, events, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["queries"]["timeouts"], 1)
        self.assertGreater(report["queries"]["succeeded"], 0)
        self.assertTrue(report["checks"]["duration_completed"])
        self.assertFalse(report["checks"]["no_query_timeouts"])
        self.assertTrue(any(event["event"] == "query_timeout" for event in events))
        self.assertGreaterEqual(len(report["samples"]), 4)

    def test_capture_failure_keeps_completed_temperature_samples(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, fail_at=102.5)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertGreaterEqual(len(report["samples"]), 2)
        self.assertFalse(report["checks"]["capture_healthy"])
        self.assertIn("simulated acquisition failure", json.dumps(report["errors"]))
        self.assertEqual(camera.stop_calls, 1)

    def test_wrong_frame_is_counted_as_query_error_and_stops_test(self):
        class WrongFrameCamera(SimulatedCamera):
            def get_before(self, timestamp_ns, timeout=1, max_age_ns=None):
                return self.snapshot()[-1]  # Equal to the signal, never an eligible predecessor.

        clock = SimulatedClock()
        camera = WrongFrameCamera(clock)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["checks"]["strict_predecessor"])
        self.assertEqual(report["queries"]["attempted"], 1)
        self.assertEqual(report["queries"]["errors"], 1)
        self.assertEqual(report["queries"]["succeeded"], 0)
        self.assertEqual(camera.stop_calls, 1)
        self.assertEqual(len(report["samples"]), 1)

    def test_late_acquisition_error_cannot_report_success(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, late_failure=True)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertIn("late acquisition failure", json.dumps(report["errors"]))

    def test_cleanup_timeout_cannot_report_success(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, cleanup_failure=True)
        report, events, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["checks"]["clean_shutdown"])
        self.assertGreater(len(report["samples"]), 0)
        self.assertTrue(any(event["event"] == "cleanup_error" for event in events))

    def test_keyboard_interrupt_preserves_report_and_stops_camera(self):
        clock = SimulatedClock(interrupt_at=102.1)
        report, events, camera = self.run_simulation(clock=clock)
        self.assertEqual(report["status"], "interrupted")
        self.assertGreater(len(report["samples"]), 0)
        self.assertFalse(report["checks"]["duration_completed"])
        self.assertEqual(camera.stop_calls, 1)
        self.assertTrue(any(event["event"] == "interrupted" for event in events))

    def test_missing_temperature_is_explicit_and_not_success(self):
        report, _, _ = self.run_simulation(probe=lambda: telemetry(None, None))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["checks"]["temperature_available"])
        self.assertEqual(report["summary"]["temperature_c"]["sample_count"], 0)
        self.assertIsNone(report["summary"]["temperature_c"]["max"])
        self.assertTrue(report["checks"]["duration_completed"])
        self.assertTrue(report["warnings"])

    def test_fill_timeout_stops_camera_and_keeps_baseline(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, never_fill=True)
        report, _, _ = self.run_simulation(clock=clock, camera=camera, fill_timeout=0.25)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["baseline_telemetry"]["temperature_c"], 50)
        self.assertFalse(report["checks"]["full_capacity"])
        self.assertEqual(camera.stop_calls, 1)

    def test_new_throttle_history_excludes_preexisting_baseline_bits(self):
        values = iter([telemetry(0x10000, 30)])
        report, _, _ = self.run_simulation(
            probe=lambda: next(values, telemetry(0x50000, 60)))
        self.assertEqual(report["baseline_telemetry"]["temperature_c"], 30)
        self.assertEqual(report["summary"]["new_throttled_history_bits"], "0x40000")
        self.assertEqual(report["summary"]["temperature_c"]["min"], 60)
        self.assertEqual(report["summary"]["temperature_c"]["max"], 60)

    def test_missing_baseline_does_not_treat_existing_history_as_new(self):
        values = iter([telemetry(None)])
        report, _, _ = self.run_simulation(
            probe=lambda: next(values, telemetry(0x50000)))
        self.assertIsNone(report["summary"]["new_throttled_history_bits"])
        self.assertTrue(any(warning["reason"] == "throttled_baseline_unavailable"
                            for warning in report["warnings"]))

    def test_current_event_is_visible_when_its_history_bit_already_existed(self):
        values = iter([telemetry(0x10000)])
        current = telemetry(0x10001)
        current["throttled_current"] = {"under_voltage": True}
        current["throttled_history"] = {"under_voltage": True}
        report, _, _ = self.run_simulation(probe=lambda: next(values, current))
        self.assertEqual(report["summary"]["new_throttled_history_bits"], "0x0")
        self.assertEqual(report["summary"]["observed_current_throttled_bits"], "0x1")
        self.assertTrue(report["samples"][0]["throttled_current"]["under_voltage"])

    def test_cleanup_error_does_not_replace_capture_error_or_samples(self):
        clock = SimulatedClock()
        camera = SimulatedCamera(clock, fail_at=102.5, cleanup_failure=True)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertGreaterEqual(len(report["samples"]), 2)
        errors = json.dumps(report["errors"])
        self.assertIn("simulated acquisition failure", errors)
        self.assertIn("simulated cleanup timeout", errors)

    def test_terminal_partial_query_window_does_not_create_artificial_timeout(self):
        class RequiresFullQueryWindow(SimulatedCamera):
            def get_before(self, timestamp_ns, timeout=1, max_age_ns=None):
                if timeout < 2:
                    self.query_calls += 1
                    raise FrameUnavailable("timeout", "artificial timeout from shortened deadline")
                return super().get_before(timestamp_ns, timeout, max_age_ns)

        clock = SimulatedClock()
        camera = RequiresFullQueryWindow(clock)
        report, _, _ = self.run_simulation(
            clock=clock, camera=camera, duration=2.25, query_interval=1, query_timeout=2)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["queries"]["attempted"], 1)
        self.assertEqual(report["queries"]["timeouts"], 0)
        self.assertGreaterEqual(report["summary"]["elapsed_seconds"], 2.25)
        self.assertLess(clock.sleep_calls, 100)

    def test_stalled_frame_counter_fails_instead_of_running_to_duration(self):
        class StalledCamera(SimulatedCamera):
            def stats(self):
                result = super().stats()
                result.update(total_appended=30, evicted=0)
                return result

        clock = SimulatedClock()
        camera = StalledCamera(clock)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["checks"]["capture_healthy"])
        self.assertFalse(report["checks"]["duration_completed"])
        self.assertIn("capture_stalled", json.dumps(report["errors"]))
        self.assertEqual(camera.stop_calls, 1)

    def test_startup_failure_still_attempts_cleanup_and_preserves_baseline(self):
        class StartupFailure(SimulatedCamera):
            def start(self):
                raise FrameUnavailable("timeout", "simulated startup timeout")

        clock = SimulatedClock()
        camera = StartupFailure(clock)
        report, _, _ = self.run_simulation(clock=clock, camera=camera)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["baseline_telemetry"]["temperature_c"], 50)
        self.assertEqual(camera.stop_calls, 1)
        self.assertIn("simulated startup timeout", json.dumps(report["errors"]))

    def test_command_returns_expected_exit_code_and_writes_each_report(self):
        settings = CameraSettings(66657, 4.87619, (3.04152, 1.50356))
        for status, expected_exit in (("passed", 0), ("failed", 1), ("interrupted", 130)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                log_path = Path(directory) / "events.jsonl"
                fake_report = {"status": status, "samples": [{"temperature_c": 55}]}
                with patch.object(runner.CameraSettings, "from_json", return_value=settings), \
                        patch.object(runner, "Picamera2FrameCapture") as factory, \
                        patch.object(runner, "run_soak", return_value=fake_report):
                    result = runner.main(["--report", str(report_path), "--log", str(log_path)])
                self.assertEqual(result, expected_exit)
                self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), fake_report)
                config = factory.call_args.args[0]
                self.assertEqual(config.capacity, 30)
                self.assertIsNone(config.width)
                self.assertIsNone(config.height)
                self.assertEqual(config.frame_timeout, 3)


class SystemTelemetryTests(unittest.TestCase):
    @staticmethod
    def read_fake(path):
        if str(path).endswith("/temp"):
            return "54321\n"
        if str(path).endswith("/status"):
            return "Name:\tpython3\nVmRSS:\t1024 kB\nVmHWM:\t2048 kB\n"
        raise FileNotFoundError(str(path))

    def test_temperature_memory_and_current_vs_historical_throttle_bits(self):
        commands = []

        def run_command(command, **kwargs):
            commands.append((command, kwargs))
            return SimpleNamespace(stdout="throttled=0x50005\n", stderr="", returncode=0)

        sample = runner.probe_system(read_text=self.read_fake, run_command=run_command)
        self.assertAlmostEqual(sample["temperature_c"], 54.321)
        self.assertEqual(sample["rss_bytes"], 1024 * 1024)
        self.assertEqual(sample["rss_peak_bytes"], 2048 * 1024)
        self.assertEqual(sample["throttled_value"], 0x50005)
        self.assertEqual(int(sample["throttled_raw"], 16), 0x50005)
        self.assertEqual(sum(sample["throttled_current"].values()), 2)
        self.assertEqual(sum(sample["throttled_history"].values()), 2)
        self.assertIsNone(sample["temperature_error"])
        self.assertIsNone(sample["rss_error"])
        self.assertIsNone(sample["throttled_error"])
        self.assertTrue(commands)
        self.assertTrue(all(call[1].get("timeout", float("inf")) <= 5
                            for call in commands))

    def test_unsupported_telemetry_is_reported_as_missing_not_zero(self):
        def absent(*args, **kwargs):
            raise FileNotFoundError("simulated unsupported telemetry")

        sample = runner.probe_system(read_text=absent, run_command=absent)
        for field in ("temperature_c", "rss_bytes", "throttled_value", "throttled_raw"):
            self.assertIsNone(sample[field])
        self.assertTrue(sample["temperature_error"])
        self.assertTrue(sample["rss_error"])
        self.assertTrue(sample["throttled_error"])

    def test_invalid_throttle_reply_does_not_claim_no_throttling(self):
        sample = runner.probe_system(
            read_text=self.read_fake,
            run_command=lambda *args, **kwargs: SimpleNamespace(
                stdout="command not supported", stderr="", returncode=0),
        )
        self.assertEqual(sample["temperature_c"], 54.321)
        self.assertIsNone(sample["throttled_value"])
        self.assertTrue(sample["throttled_error"])


if __name__ == "__main__":
    unittest.main()
