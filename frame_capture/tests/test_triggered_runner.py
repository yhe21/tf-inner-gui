"""Triggered hardware-runner result checks using synthetic frames and time."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from frame_capture import CameraSettings, FrameRecord, FrameUnavailable
from frame_capture.tests.fakes import FakeRequest
from frame_capture.tests.test_soak import SimulatedClock


_PATH = Path(__file__).resolve().parents[2] / "tools" / "test_triggered_capture.py"
_SPEC = importlib.util.spec_from_file_location("triggered_capture_test_runner", _PATH)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class SimulatedSession:
    def __init__(self, clock, *, cleanup_delay=0.0, never_finish=False):
        self.clock = clock
        self.cleanup_delay = cleanup_delay
        self.never_finish = never_finish
        self.inner_finished = None
        self.completed = False
        self.closed = False
        self.start_delay = 0.1
        self.max_duration = 0.1
        self.observed = []
        self.error = None

    def stats(self):
        elapsed = (0.0 if self.inner_finished is None else
                   self.clock.monotonic() - self.inner_finished)
        result = {"state": "idle", "error": self.error, "cleanup_error": None,
                  "stop_reason": None, "current_capture": None, "last_capture": None}
        if self.closed:
            result["state"] = "closed"
        elif self.completed:
            result["stop_reason"] = "explicit_stop"
        elif elapsed >= self.start_delay + self.max_duration:
            result["stop_reason"] = "duration_limit"
            result["state"] = ("stopping" if self.never_finish or
                               elapsed < self.start_delay + self.max_duration + self.cleanup_delay
                               else "idle")
            result["last_capture"] = {"stop_reason": "duration_limit"}
        elif elapsed >= self.start_delay:
            result["state"] = "running"
        self.observed.append(dict(result))
        return result


class SimulatedWorkflow:
    def __init__(self, session, *, bad_exposure=False, cleanup_failure=False,
                 late_capture_failure=False, final_capture_failure=False):
        self.session = session
        self.bad_exposure = bad_exposure
        self.cleanup_failure = cleanup_failure
        self.late_capture_failure = late_capture_failure
        self.final_capture_failure = final_capture_failure
        self.glue_calls = 0
        self.close_calls = 0
        self.close_time = None

    def make_frame(self, timestamp, exposure=5000):
        request = FakeRequest(timestamp, exposure_us=exposure)
        return FrameRecord(0, timestamp, request.image, request.metadata)

    def on_inner(self):
        self.session.inner_finished = self.session.clock.monotonic()
        return self.make_frame(self.session.clock.signal(),
                               50000 if self.bad_exposure else 5000)

    def on_glue(self, timestamp):
        self.glue_calls += 1
        self.session.completed = True
        if self.late_capture_failure:
            self.session.error = "late acquisition failure after selected frame"
        return self.make_frame(timestamp - 1)

    def stats(self):
        return {"state": "closed" if self.session.closed else "buffering",
                "error": None, "session": self.session.stats()}

    def close(self):
        self.close_calls += 1
        self.close_time = self.session.clock.monotonic()
        self.session.closed = True
        if self.final_capture_failure:
            self.session.error = "final acquisition failure during shutdown"
        if self.cleanup_failure:
            raise FrameUnavailable("cleanup_failed", "simulated camera close failed")


class TriggeredRunnerTests(unittest.TestCase):
    def run_simulation(self, *, skip_glue=False, cleanup_delay=0, never_finish=False,
                       bad_exposure=False, cleanup_failure=False,
                       late_capture_failure=False, final_capture_failure=False, temperature=50):
        clock = SimulatedClock()
        session = SimulatedSession(clock, cleanup_delay=cleanup_delay,
                                   never_finish=never_finish)
        workflow = SimulatedWorkflow(
            session, bad_exposure=bad_exposure, cleanup_failure=cleanup_failure,
            late_capture_failure=late_capture_failure,
            final_capture_failure=final_capture_failure)
        settings = CameraSettings(5000, 2.0, (1.5, 1.6))
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            log_path = Path(directory) / "events.jsonl"
            arguments = ["--cycles", "1", "--start-delay", "0.1", "--max-duration", "0.1",
                         "--glue-delay", "0.15", "--report", str(report_path),
                         "--log", str(log_path)]
            if skip_glue:
                arguments.append("--skip-glue")
            with (patch.object(runner, "time", SimpleNamespace(
                    monotonic=clock.monotonic, sleep=clock.sleep)),
                  patch.object(runner, "now_sensor_clock_ns", clock.signal),
                  patch.object(runner.CameraSettings, "from_json", return_value=settings),
                  patch.object(runner, "OnDemandFrameCapture", return_value=session),
                  patch.object(runner, "TriggeredFrameCapture", return_value=workflow),
                  patch.object(runner, "probe_system", return_value={"temperature_c": temperature}),
                  patch("builtins.print")):
                code = runner.main(arguments)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        return code, report, events, session, workflow

    def test_normal_cycle_checks_frames_and_closes_cleanly(self):
        code, report, events, _, workflow = self.run_simulation()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["cycles_completed"], 1)
        self.assertEqual(workflow.glue_calls, 1)
        self.assertEqual(workflow.close_calls, 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(events[-1]["event"], "finished")
        self.assertEqual(events[-1]["status"], "passed")

    def test_skip_glue_handles_initial_missing_last_capture_then_waits_for_release(self):
        code, report, _, session, workflow = self.run_simulation(
            skip_glue=True, cleanup_delay=0.7)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(any(row["last_capture"] is None for row in session.observed))
        self.assertTrue(any(row["state"] == "stopping" for row in session.observed))
        self.assertGreaterEqual(workflow.close_time - session.inner_finished, 0.9)
        self.assertTrue(report["cycles"][0]["automatic_stop"])
        self.assertEqual(workflow.glue_calls, 0)

    def test_duration_limit_does_not_pass_while_camera_is_still_stopping(self):
        code, report, events, _, _ = self.run_simulation(skip_glue=True, never_finish=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("did not automatically stop" in error for error in report["errors"]))
        self.assertNotIn("automatic_stop", report["cycles"][0])
        self.assertFalse(any(row["event"] == "cycle_complete" for row in events))

    def test_cleanup_failure_cannot_leave_successful_report(self):
        code, report, events, _, _ = self.run_simulation(cleanup_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("simulated camera close failed" in error for error in report["errors"]))
        self.assertEqual(events[-1]["status"], "failed")

    def test_first_single_exposure_mismatch_fails_before_glue(self):
        code, report, _, _, workflow = self.run_simulation(bad_exposure=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("exposure does not match" in error for error in report["errors"]))
        self.assertEqual(workflow.glue_calls, 0)
        self.assertEqual(workflow.close_calls, 1)
        self.assertEqual(report["summary"]["cycles_completed"], 0)

    def test_missing_temperature_is_explicitly_reported_as_warning(self):
        code, report, _, _, _ = self.run_simulation(temperature=None)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertIsNone(report["summary"]["temperature_max_c"])
        self.assertTrue(any("thermal benefit is unverified" in row for row in report["warnings"]))

    def test_late_capture_failure_after_selection_cannot_pass(self):
        code, report, _, _, _ = self.run_simulation(late_capture_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("late acquisition failure" in error for error in report["errors"]))

    def test_failure_visible_only_in_final_statistics_cannot_pass(self):
        code, report, _, _, _ = self.run_simulation(final_capture_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("final acquisition failure" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
