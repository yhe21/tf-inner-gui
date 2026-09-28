"""Camera recovery integration checks without a camera, robot, or reboot."""

import os
import logging
import multiprocessing
import sys
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtCore, QtNetwork, QtWidgets  # noqa: E402

import main  # noqa: E402
from inspection import InspectionResult  # noqa: E402

from main import (  # noqa: E402
    APP_DIR,
    CameraController,
    CameraMonitorDialog,
    CaptureWorker,
    ProcessCaptureWorker,
    Vt6TrainingServer,
)


MANUAL_SETTINGS = {
    "exposure_time_us": 12000,
    "analogue_gain": 1.25,
    "colour_gains": [1.7, 1.4],
}


class CameraRecoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_busy_camera_page_can_exit_without_stopping_camera_and_disconnects(self):
        signal_names = (
            "status_changed", "capture_started", "frame_acquired",
            "capture_succeeded", "capture_failed", "inspection_status_changed",
            "inspection_completed", "inspection_failed", "auto_calibration_started",
            "auto_calibration_succeeded", "auto_calibration_failed",
        )
        for exit_method in ("back", "reject", "close"):
            with self.subTest(exit_method=exit_method):
                controller = CameraController()
                controller.worker = mock.Mock()
                controller.stop = mock.Mock()
                controller.on_ready(4056, 3040, True)
                controller.busy = True
                dialog = CameraMonitorDialog(controller)
                dialog.show()
                self.app.processEvents()
                try:
                    self.assertTrue(dialog.btnCameraBack.isEnabled())
                    self.assertFalse(dialog.btnManualCapture.isEnabled())
                    for name in signal_names:
                        self.assertEqual(controller.receivers(getattr(controller, name)), 1)

                    if exit_method == "back":
                        dialog.btnCameraBack.click()
                    elif exit_method == "reject":
                        dialog.reject()
                    else:
                        self.assertTrue(dialog.close())
                    self.app.processEvents()

                    self.assertFalse(dialog.isVisible())
                    self.assertTrue(controller.busy)
                    self.assertTrue(controller.ready)
                    controller.stop.assert_not_called()
                    controller.worker.stop.assert_not_called()
                    for name in signal_names:
                        self.assertEqual(controller.receivers(getattr(controller, name)), 0)
                finally:
                    dialog.close()

    def test_recovery_keeps_back_available_without_modal_capture_or_calibration_error(self):
        controller = CameraController(initial_camera_settings=MANUAL_SETTINGS)
        controller.on_ready(4056, 3040, True)
        controller.busy = True
        dialog = CameraMonitorDialog(controller)
        try:
            recovery_message = "Camera unavailable - retry 1/10"
            with mock.patch.object(QtWidgets.QMessageBox, "critical") as critical:
                controller.on_camera_unavailable(recovery_message)
                controller.on_capture_failed("Frame acquisition timed out")
                controller.on_auto_calibration_failed("Frame acquisition timed out")

            critical.assert_not_called()
            self.assertTrue(dialog.btnCameraBack.isEnabled())
            self.assertFalse(dialog.btnManualCapture.isEnabled())
            self.assertFalse(dialog.btnAutoExposure.isEnabled())
            self.assertEqual(dialog.lblCameraPageStatus.text(), recovery_message)
            self.assertFalse(controller.busy)
            self.assertFalse(controller.ready)
            self.assertEqual(controller.camera_settings, MANUAL_SETTINGS)

            controller.on_ready(4056, 3040, True)
            self.assertFalse(controller.recovering)
            self.assertTrue(dialog.btnManualCapture.isEnabled())
            self.assertTrue(dialog.btnAutoExposure.isEnabled())
        finally:
            dialog.close()

    def test_failed_saved_image_preview_does_not_leave_capture_controls_disabled(self):
        controller = CameraController()
        controller.on_ready(4056, 3040, True)
        controller.busy = True
        dialog = CameraMonitorDialog(controller)
        try:
            self.assertFalse(dialog.btnManualCapture.isEnabled())
            # A saved file may disappear or be unreadable before preview decoding.
            missing_image = APP_DIR / "captures" / "missing-recovery-test-image.jpg"
            controller.on_capture_succeeded(str(missing_image), True)

            self.assertIn("preview could not be loaded", dialog.lblCapturedImage.text())
            self.assertIsNone(dialog._preview_image_path)
            self.assertFalse(controller.busy)
            self.assertTrue(dialog.btnManualCapture.isEnabled())
            self.assertTrue(dialog.btnAutoExposure.isEnabled())
            self.assertTrue(dialog.btnCameraBack.isEnabled())
            self.assertTrue(dialog.chkSaveProductionImages.isEnabled())
        finally:
            dialog.close()


class BufferedClient:
    """Exercise the real protocol's session guard without a listening socket."""

    def __init__(self):
        self.writes = []

    def state(self):
        return QtNetwork.QAbstractSocket.ConnectedState

    def write(self, data):
        self.writes.append(bytes(data).decode("ascii").strip())
        return len(data)

    def flush(self):
        return True


class CameraRecoveryProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.error_root = Path(self.temporary_directory.name) / "error_records"
        self.controller = CameraController()
        self.controller.worker = mock.Mock()
        self.controller.on_ready(4056, 3040, True)
        self.calibration = {
            "PickNP": {"X": 0.05, "Y": -0.10, "Z": 0.0, "U": 0.05},
            "PickNPS": {"X": 0.0, "Y": 0.0, "Z": 0.05, "U": -0.05},
            "DropNP": {"X": -0.10, "Y": 0.0, "Z": 0.0, "U": 0.10},
        }
        self.server = Vt6TrainingServer(
            self.controller, lambda: self.calibration,
            port=0, error_root=self.error_root,
        )
        self.client = BufferedClient()
        self.server.current_client = self.client
        self.server.current_session_id = 7

    def test_unavailable_discards_queued_jobs_and_fails_active_job_only_once(self):
        self.server.handle_command("INNER", response_session=7)
        active_path = self.server.active_capture[1]
        self.server.handle_command("GLUE", response_session=7)
        self.server.handle_command("NP", response_session=7)
        self.server.handle_command("PICK_NP_NO_VAC")
        self.assertEqual(len(self.server.capture_queue), 3)
        self.assertEqual(self.controller.worker.request_capture.call_count, 1)

        self.controller.on_camera_unavailable("Camera recovering")

        self.assertFalse(self.server.capture_queue)
        self.assertEqual(self.client.writes, ["GLUE,OK", "NP,OK"])
        self.assertEqual(self.server.active_capture[1], active_path)
        log = (self.error_root / "error.log").read_text(encoding="utf-8")
        self.assertIn("PICK_NP_NO_VAC\tCAMERA_NOT_READY\t", log)

        self.controller.on_capture_failed("Frame timeout")
        self.controller.on_capture_failed("Late duplicate failure")
        self.assertIsNone(self.server.active_capture)
        self.assertEqual(self.client.writes, ["GLUE,OK", "NP,OK", "INNER,OK"])

        self.controller.on_ready(4056, 3040, True)
        self.assertEqual(self.controller.worker.request_capture.call_count, 1)
        self.assertFalse(self.server.capture_queue)

    def test_recovery_drops_old_session_replies_for_active_and_queued_jobs(self):
        self.server.handle_command("INNER", response_session=7)
        self.server.handle_command("GLUE", response_session=7)
        self.server.current_session_id = 8
        self.server.handle_command("NP", response_session=8)

        self.controller.on_camera_unavailable("Camera recovering")
        self.controller.on_capture_failed("Frame timeout")

        self.assertEqual(self.client.writes, ["NP,OK"])
        self.assertFalse(self.server.capture_queue)
        self.assertIsNone(self.server.active_capture)

    def test_active_fault_is_logged_failed_once_and_never_gets_robot_reply(self):
        self.server.handle_command("PICK_NP_NO_VAC")
        self.assertIsNotNone(self.server.active_capture)

        self.controller.on_camera_unavailable("Camera recovering")
        self.controller.on_capture_failed("Frame timeout")
        self.controller.on_capture_failed("Late duplicate failure")

        log = (self.error_root / "error.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("CAPTURE_FAILED:"), 1)
        self.assertIn("CAPTURE_FAILED: Frame timeout", log)
        self.assertEqual(self.client.writes, [])
        self.assertIsNone(self.server.active_capture)

    def test_new_triggers_while_unavailable_keep_forced_ok_and_calibration_protocol(self):
        self.controller.on_camera_unavailable("Camera recovering")
        for command in ("INNER", "GLUE", "NP"):
            self.server.handle_command(command, response_session=7)
        self.server.handle_command("CALIB", response_session=7)
        self.server.handle_command("PICK_NP_NO_VAC")

        self.assertEqual(self.client.writes, [
            "INNER,OK", "GLUE,OK", "NP,OK",
            "+0.05,-0.10,+0.00,+0.05,+0.00,+0.00,+0.05,-0.05,"
            "-0.10,+0.00,+0.00,+0.10",
        ])
        self.controller.worker.request_capture.assert_not_called()
        self.assertIsNone(self.server.active_capture)
        self.assertFalse(self.server.capture_queue)
        log = (self.error_root / "error.log").read_text(encoding="utf-8")
        self.assertIn("PICK_NP_NO_VAC\tRECEIVED\t", log)
        self.assertIn("PICK_NP_NO_VAC\tCAMERA_NOT_READY\t", log)


class StartupProbeCamera:
    sensor_resolution = (4056, 3040)

    def __init__(self, events, fail_at=None):
        self.events = events
        self.fail_at = fail_at

    def create_still_configuration(self, **options):
        return options

    def configure(self, configuration):
        self.events.append("configure")

    def set_controls(self, controls):
        self.events.append("manual_controls")

    def start(self):
        self.events.append("start")

    def capture_request(self, flush):
        self.events.append(("acquire", flush))
        if self.fail_at == "acquire":
            raise RuntimeError("No first frame")
        return self

    def get_metadata(self):
        self.events.append("metadata")
        if self.fail_at == "metadata":
            raise RuntimeError("Bad first-frame metadata")
        return {"SensorTimestamp": 123456789}

    def release(self):
        self.events.append("release")

    def stop(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


class CameraStartupProbeTests(unittest.TestCase):
    def test_startup_ready_is_emitted_only_after_first_frame_is_released(self):
        for settings in (None, MANUAL_SETTINGS):
            with self.subTest(manual_settings=settings is not None):
                events = []
                camera = StartupProbeCamera(events)
                worker = CaptureWorker(
                    lambda: camera, warmup_seconds=0,
                    initial_camera_settings=settings, validate_startup=True,
                )
                worker.ready.connect(lambda *args: events.append(("ready", args)))
                worker.startup_probe_succeeded.connect(lambda: events.append("startup_probe"))
                worker.stop()
                worker.run()

                expected_ready = ("ready", (4056, 3040, settings is not None))
                self.assertEqual(events.count(("acquire", True)), 1)
                self.assertLess(events.index("start"), events.index(("acquire", True)))
                self.assertLess(events.index("metadata"), events.index("release"))
                self.assertLess(events.index("release"), events.index("startup_probe"))
                self.assertLess(events.index("startup_probe"), events.index(expected_ready))
                self.assertEqual(events[-2:], ["stop", "close"])

    def test_startup_probe_failure_never_marks_camera_ready_and_releases_acquired_frame(self):
        for fail_at in ("acquire", "metadata"):
            with self.subTest(fail_at=fail_at):
                events = []
                camera = StartupProbeCamera(events, fail_at=fail_at)
                worker = CaptureWorker(
                    lambda: camera, warmup_seconds=0,
                    initial_camera_settings=MANUAL_SETTINGS, validate_startup=True,
                )
                ready = []
                failed = []
                worker.ready.connect(lambda *args: ready.append(args))
                worker.initialization_failed.connect(failed.append)
                worker.stop()
                worker.run()

                self.assertEqual(ready, [])
                self.assertEqual(len(failed), 1)
                self.assertEqual("release" in events, fail_at == "metadata")
                self.assertEqual(events[-2:], ["stop", "close"])

    def test_missing_camera_never_emits_ready(self):
        worker = CaptureWorker(
            mock.Mock(side_effect=RuntimeError("No cameras available")),
            warmup_seconds=0, initial_camera_settings=MANUAL_SETTINGS,
            validate_startup=True,
        )
        ready = []
        failed = []
        worker.ready.connect(lambda *args: ready.append(args))
        worker.initialization_failed.connect(failed.append)
        worker.stop()
        worker.run()
        self.assertEqual(ready, [])
        self.assertEqual(failed, ["No cameras available"])


def run_camera_child_with_fake_hardware(connection, config):
    """Real production child entry point; only the camera and image writer differ."""
    if QtCore.QCoreApplication.instance() is not None:
        raise AssertionError("The spawned camera worker must not need a Qt application")
    camera = StartupProbeCamera([])

    def save_fake_image(request, output_path):
        if not isinstance(output_path, Path):
            raise AssertionError("Capture Path did not survive the process boundary")
        if request.events.count(("acquire", True)) != 2:
            raise AssertionError("Startup must acquire a distinct probe before capture")
        if request.events.count("release") != 1:
            raise AssertionError("The startup probe must be released before capture")
        output_path.write_bytes(b"fake image from a separate camera process")

    with mock.patch.object(main, "CaptureWorker", partial(
        CaptureWorker, camera_factory=lambda: camera, warmup_seconds=0,
        image_saver=save_fake_image,
    )):
        main.run_camera_child(connection, config)


class SpawnedCameraIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def receive_until(self, connection, target_event, timeout=8):
        events = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if connection.poll(min(0.1, max(0, deadline - time.monotonic()))):
                event = connection.recv()
                events.append(event)
                if event[0] == target_event:
                    return events
        self.fail(f"Child did not emit {target_event!r}; received {events!r}")

    def test_real_child_signals_work_without_qapplication_and_pickle_capture_results(self):
        context = multiprocessing.get_context("spawn")
        parent_pipe, child_pipe = context.Pipe()
        process = context.Process(
            target=run_camera_child_with_fake_hardware,
            args=(child_pipe, {"settings": MANUAL_SETTINGS, "model_root": None}),
            daemon=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "captured.jpg"
            process.start()
            child_pipe.close()
            try:
                startup_events = self.receive_until(parent_pipe, "ready")
                self.assertEqual(startup_events, [
                    ("startup_probe_succeeded", ()), ("ready", (4056, 3040, True)),
                ])
                parent_pipe.send(("capture", output_path, True, "INNER", True))
                capture_events = self.receive_until(parent_pipe, "succeeded")
                self.assertEqual([name for name, _args in capture_events], [
                    "capture_started", "frame_acquired", "inspection_completed", "succeeded",
                ])
                result = capture_events[2][1][0]
                self.assertIsInstance(result, InspectionResult)
                self.assertEqual(result.command, "INNER")
                self.assertTrue(result.was_bypassed)
                self.assertEqual(capture_events[-1], ("succeeded", (str(output_path), True)))
                self.assertEqual(output_path.read_bytes(), b"fake image from a separate camera process")

                parent_pipe.send(None)
                self.assertEqual(self.receive_until(parent_pipe, "stopped"), [("stopped", ())])
                process.join(3)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(3)
                parent_pipe.close()
                process.close()

    def test_controller_worker_thread_supervisor_and_spawned_child_complete_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "captured.jpg"
            logger = logging.Logger("camera-recovery-integration-test")
            logger.addHandler(logging.NullHandler())
            worker = ProcessCaptureWorker(initial_camera_settings=MANUAL_SETTINGS)
            controller = CameraController(worker_factory=lambda: worker)
            statuses = []
            completions = []
            failures = []
            results = []
            stopped = []
            controller.status_changed.connect(lambda *args: statuses.append(args))
            controller.capture_succeeded.connect(lambda *args: completions.append(args))
            controller.capture_failed.connect(failures.append)
            controller.inspection_completed.connect(results.append)
            worker.stopped.connect(lambda: stopped.append(True))

            with mock.patch.object(main, "run_camera_child", run_camera_child_with_fake_hardware), \
                 mock.patch.object(main.logging, "getLogger", return_value=logger):
                controller.start()
                deadline = time.monotonic() + 8
                try:
                    while not controller.ready and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(0.005)
                    self.assertTrue(controller.ready, statuses)
                    self.assertTrue(controller.capture(
                        output_path, save_image=True, inspection_kind="INNER", bypass_inspection=True,
                    ))
                    self.assertTrue(controller.busy)
                    while not completions and not failures and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(0.005)
                    self.assertEqual(failures, [])
                    self.assertEqual(completions, [(str(output_path), True)])
                    self.assertFalse(controller.busy)
                    self.assertEqual(len(results), 1)
                    self.assertIsInstance(results[0], InspectionResult)
                    self.assertTrue(results[0].was_bypassed)
                    self.assertEqual(output_path.read_bytes(), b"fake image from a separate camera process")
                finally:
                    self.assertTrue(controller.stop(timeout_ms=4000))
                    self.app.processEvents()
                self.assertEqual(stopped, [True])
                self.assertFalse(controller.thread.isRunning())

    def test_recovery_log_directory_failure_does_not_prevent_worker_stopped_signal(self):
        logger = logging.Logger("camera-recovery-log-failure-test")
        worker = ProcessCaptureWorker(initial_camera_settings=MANUAL_SETTINGS)
        stopped = []
        worker.stopped.connect(lambda: stopped.append(True))
        # The sentinel prevents any camera process from being launched in this test.
        worker.stop()
        with mock.patch.object(main.logging, "getLogger", return_value=logger), \
             mock.patch.object(main.Path, "mkdir", side_effect=PermissionError("Read-only state directory")):
            worker.run()
        self.assertEqual(stopped, [True])


if __name__ == "__main__":
    unittest.main()
