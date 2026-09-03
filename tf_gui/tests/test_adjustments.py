import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtNetwork, QtWidgets  # noqa: E402

from inspection import InspectionResult, SidePrediction  # noqa: E402

from main import (  # noqa: E402
    AdjustmentDialog,
    AdjustmentStore,
    CameraController,
    CameraMonitorDialog,
    CameraSettingsStore,
    CaptureSettingsStore,
    CaptureWorker,
    CAMERA_BUFFER_COUNT,
    MAX_VALUE,
    MIN_VALUE,
    STEP,
    Vt6TrainingServer,
    build_capture_path,
    build_error_capture_path,
    save_counterclockwise_rotated_jpeg,
)


class AdjustmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_dialog_clamps_values_and_disables_limit_buttons(self) -> None:
        dialog = AdjustmentDialog(
            "PickNP", {"X": 0.0, "Y": 0.0, "Z": 0.0, "U": 0.0}
        )

        for _ in range(20):
            dialog.adjust_axis("X", STEP)
        self.assertEqual(dialog.values()["X"], MAX_VALUE)
        self.assertFalse(dialog.btnXPlus.isEnabled())

        for _ in range(40):
            dialog.adjust_axis("X", -STEP)
        self.assertEqual(dialog.values()["X"], MIN_VALUE)
        self.assertFalse(dialog.btnXMinus.isEnabled())

    def test_three_stations_are_saved_and_loaded_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "adjustments.json"
            store = AdjustmentStore(path)
            values = store.load()
            values["PickNP"]["X"] = 0.15
            values["PickNPS"]["Y"] = -0.25
            values["DropNP"]["U"] = 0.50
            store.save(values)

            loaded = AdjustmentStore(path).load()
            self.assertEqual(loaded["PickNP"]["X"], 0.15)
            self.assertEqual(loaded["PickNPS"]["Y"], -0.25)
            self.assertEqual(loaded["DropNP"]["U"], 0.50)

    def test_capture_path_uses_date_folder_and_timestamp_filename(self) -> None:
        captured_at = datetime(2026, 8, 19, 14, 5, 6, 123000)
        path = build_capture_path(captured_at, Path("captures"))
        self.assertEqual(
            path.as_posix(),
            "captures/20260819/20260819_140506_123.jpg",
        )
        categorized_path = build_capture_path(
            captured_at, Path("captures"), category="inner"
        )
        self.assertEqual(
            categorized_path.as_posix(),
            "captures/20260819/INNER/20260819_140506_123.jpg",
        )

    def test_error_capture_path_uses_one_flat_folder(self) -> None:
        captured_at = datetime(2026, 8, 21, 14, 5, 6, 123000)
        path = build_error_capture_path(
            "pick np no vac", captured_at, Path("error_records")
        )
        self.assertEqual(
            path.as_posix(),
            "error_records/20260821_140506_123_PICK_NP_NO_VAC.jpg",
        )

    def test_camera_page_loads_without_picamera_on_pc(self) -> None:
        controller = CameraController()
        dialog = CameraMonitorDialog(controller)
        self.assertEqual(dialog.btnManualCapture.text(), "Capture and Save")
        self.assertTrue(dialog.chkSaveProductionImages.isChecked())
        self.assertFalse(dialog.chkBypassInspection.isChecked())
        self.assertEqual(dialog.lblInnerResult.text(), "INNER: Not tested")
        self.assertEqual(dialog.lblGlueResult.text(), "GLUE: Not tested")
        self.assertFalse(dialog.capture_is_running())

    def test_camera_page_shows_latest_inspection_result(self) -> None:
        controller = CameraController()
        dialog = CameraMonitorDialog(controller)
        result = InspectionResult(
            command="INNER",
            overall_label="NG",
            left=SidePrediction("OK", 0.9876),
            right=SidePrediction("NG", 0.9123),
            elapsed_ms=456.7,
        )

        controller.on_inspection_completed(result)

        self.assertIn("INNER: NG", dialog.lblInnerResult.text())
        self.assertIn("L OK 98.8%", dialog.lblInnerResult.text())
        self.assertIn("R NG 91.2%", dialog.lblInnerResult.text())
        self.assertIn("AI 457 ms", dialog.lblInnerResult.text())
        self.assertEqual(dialog.lblInnerResult.property("inspectionState"), "NG")

        controller.on_inspection_completed(InspectionResult.forced_ok("GLUE"))
        self.assertEqual(
            dialog.lblGlueResult.text(),
            "GLUE: OK | INSPECTION BYPASSED",
        )
        self.assertEqual(dialog.lblGlueResult.property("inspectionState"), "OK")

    def test_production_save_choice_is_emitted_and_persisted(self) -> None:
        controller = CameraController()
        dialog = CameraMonitorDialog(
            controller, production_save_enabled=False
        )
        choices = []
        bypass_choices = []
        dialog.production_save_changed.connect(choices.append)
        dialog.inspection_bypass_changed.connect(bypass_choices.append)
        dialog.chkSaveProductionImages.setChecked(True)
        dialog.chkBypassInspection.setChecked(True)
        self.assertEqual(choices, [True])
        self.assertEqual(bypass_choices, [True])
        self.assertIn("saving ON", dialog.lblCaptureMode.text())
        self.assertIn("AI BYPASS", dialog.lblCaptureMode.text())

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capture_settings.json"
            store = CaptureSettingsStore(path)
            self.assertTrue(store.load())
            self.assertFalse(store.load_bypass())
            store.save(False)
            store.save_bypass(True)
            self.assertFalse(CaptureSettingsStore(path).load())
            self.assertTrue(CaptureSettingsStore(path).load_bypass())

    def test_camera_settings_are_external_validated_and_persisted(self) -> None:
        settings = {
            "exposure_time_us": 12500,
            "analogue_gain": 1.75,
            "colour_gains": [1.42, 1.68],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "camera_settings.json"
            store = CameraSettingsStore(path)
            self.assertIsNone(store.load())

            store.save(settings)
            self.assertEqual(CameraSettingsStore(path).load(), settings)

            path.write_text('{"exposure_time_us": 0}', encoding="utf-8")
            self.assertIsNone(CameraSettingsStore(path).load())

    def test_auto_calibration_reads_metadata_then_locks_controls(self) -> None:
        class FakeRequest:
            def __init__(self) -> None:
                self.released = False

            def get_metadata(self):
                return {
                    "ExposureTime": 12000,
                    "AnalogueGain": 1.25,
                    "ColourGains": (1.70, 1.40),
                }

            def release(self):
                self.released = True

        class FakeCamera:
            def __init__(self) -> None:
                self.sensor_resolution = (4056, 3040)
                self.request = FakeRequest()
                self.controls = []

            def create_still_configuration(self, **options):
                return options

            def configure(self, _configuration):
                pass

            def set_controls(self, controls):
                self.controls.append(controls)

            def start(self):
                pass

            def capture_request(self, flush):
                self.flush = flush
                return self.request

            def stop(self):
                pass

            def close(self):
                pass

        fake_camera = FakeCamera()
        worker = CaptureWorker(
            lambda: fake_camera,
            warmup_seconds=0.0,
            auto_calibration_seconds=0.0,
            manual_settle_seconds=0.0,
        )
        ready_events = []
        calibrated_settings = []
        worker.ready.connect(
            lambda width, height, locked: ready_events.append(
                (width, height, locked)
            )
        )
        worker.auto_calibration_succeeded.connect(calibrated_settings.append)

        worker.request_auto_calibration()
        worker.stop()
        worker.run()

        expected_settings = {
            "exposure_time_us": 12000,
            "analogue_gain": 1.25,
            "colour_gains": [1.70, 1.40],
        }
        self.assertEqual(ready_events, [(4056, 3040, False)])
        self.assertEqual(calibrated_settings, [expected_settings])
        self.assertEqual(
            fake_camera.controls,
            [
                {"AeEnable": True, "AwbEnable": True},
                {
                    "AeEnable": False,
                    "AwbEnable": False,
                    "ExposureTime": 12000,
                    "AnalogueGain": 1.25,
                    "ColourGains": (1.70, 1.40),
                },
            ],
        )
        self.assertTrue(fake_camera.flush)
        self.assertTrue(fake_camera.request.released)

    def test_saved_manual_controls_are_applied_before_camera_start(self) -> None:
        events = []

        class FakeCamera:
            sensor_resolution = (4056, 3040)

            def create_still_configuration(self, **options):
                return options

            def configure(self, _configuration):
                events.append("configure")

            def set_controls(self, controls):
                events.append(("controls", controls))

            def start(self):
                events.append("start")

            def stop(self):
                pass

            def close(self):
                pass

        settings = {
            "exposure_time_us": 8000,
            "analogue_gain": 1.5,
            "colour_gains": [1.3, 1.6],
        }
        worker = CaptureWorker(
            FakeCamera,
            warmup_seconds=0.0,
            initial_camera_settings=settings,
        )
        ready_events = []
        worker.ready.connect(
            lambda width, height, locked: ready_events.append(
                (width, height, locked)
            )
        )
        worker.stop()
        worker.run()

        self.assertEqual(events[0], "configure")
        self.assertEqual(events[1][0], "controls")
        self.assertEqual(events[1][1]["AeEnable"], False)
        self.assertEqual(events[1][1]["AwbEnable"], False)
        self.assertEqual(events[1][1]["ExposureTime"], 8000)
        self.assertEqual(events[2], "start")
        self.assertEqual(ready_events, [(4056, 3040, True)])

    def test_runtime_and_ui_files_have_no_chinese_menu_text(self) -> None:
        interface_files = [PROJECT_DIR / "main.py"]
        interface_files.extend((PROJECT_DIR / "ui").glob("*.ui"))
        for interface_file in interface_files:
            self.assertNotRegex(
                interface_file.read_text(encoding="utf-8"),
                r"[\u4e00-\u9fff]",
                msg=f"Chinese UI text remains in {interface_file.name}",
            )

    def test_epson_np_trigger_precedes_pick_fixture_motion(self) -> None:
        program_text = (REPOSITORY_DIR / "robot" / "Main.prg").read_text(
            encoding="utf-8"
        )
        self.assertIn('Print #202, "NP"', program_text)
        self.assertIn('Case "NP,OK"', program_text)
        self.assertIn('Case "NP,NG"', program_text)
        self.assertIn("MemOff RpiNpReq", program_text)

        pick_fixture = program_text.split("Function Pick_Fixture", 1)[1]
        pick_fixture = pick_fixture.split("Fend", 1)[0]
        self.assertLess(
            pick_fixture.index("MemOn RpiNpReq"),
            pick_fixture.index("Move P_Drop_NP"),
        )

    def test_persistent_worker_captures_a_fresh_frame_and_releases_it(self) -> None:
        class FakeRequest:
            def __init__(self) -> None:
                self.released = False

            def get_metadata(self):
                return {"SensorTimestamp": 123456789}

            def release(self):
                self.released = True

        class FakeCamera:
            def __init__(self) -> None:
                self.sensor_resolution = (4056, 3040)
                self.configuration_options = None
                self.request = FakeRequest()
                self.flush_value = None
                self.started = False
                self.stopped = False
                self.closed = False

            def create_still_configuration(self, **options):
                self.configuration_options = options
                return options

            def configure(self, _configuration):
                pass

            def start(self):
                self.started = True

            def capture_request(self, flush):
                self.flush_value = flush
                return self.request

            def stop(self):
                self.stopped = True

            def close(self):
                self.closed = True

        fake_camera = FakeCamera()
        saved_requests = []

        def save_image(request, output_path):
            saved_requests.append((request, output_path))
            output_path.write_bytes(b"fake-jpeg")

        worker = CaptureWorker(
            lambda: fake_camera,
            warmup_seconds=0.0,
            image_saver=save_image,
        )
        acquired = []
        succeeded = []
        worker.frame_acquired.connect(
            lambda path, timestamp, saved: acquired.append(
                (path, timestamp, saved)
            )
        )
        worker.succeeded.connect(
            lambda path, saved: succeeded.append((path, saved))
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "capture.jpg"
            transient_path = Path(temporary_directory) / "transient.jpg"
            worker.request_capture(output_path)
            worker.request_capture(transient_path, save_image=False)
            worker.stop()
            worker.run()

            self.assertTrue(output_path.exists())
            self.assertFalse(transient_path.exists())
            self.assertEqual(
                succeeded,
                [(str(output_path), True), (str(transient_path), False)],
            )
            self.assertEqual(
                acquired,
                [
                    (str(output_path), 123456789, True),
                    (str(transient_path), 123456789, False),
                ],
            )

        options = fake_camera.configuration_options
        self.assertEqual(options["main"]["size"], fake_camera.sensor_resolution)
        self.assertEqual(options["buffer_count"], CAMERA_BUFFER_COUNT)
        self.assertFalse(options["queue"])
        self.assertTrue(fake_camera.flush_value)
        self.assertEqual(saved_requests, [(fake_camera.request, output_path)])
        self.assertTrue(fake_camera.request.released)
        self.assertTrue(fake_camera.stopped)
        self.assertTrue(fake_camera.closed)

    def test_saved_jpeg_is_rotated_90_degrees_counterclockwise(self) -> None:
        rotate_90 = object()
        calls = []

        class FakeRotatedImage:
            def save(self, output_path, **options):
                calls.append(("save", output_path, options))

        class FakeImage:
            def transpose(self, method):
                calls.append(("transpose", method))
                return FakeRotatedImage()

        class FakeRequest:
            def make_image(self, stream_name):
                calls.append(("make_image", stream_name))
                return FakeImage()

        pil_module = ModuleType("PIL")
        image_module = ModuleType("PIL.Image")

        class FakeTranspose:
            ROTATE_90 = rotate_90

        image_module.Transpose = FakeTranspose
        pil_module.Image = image_module

        output_path = Path("rotated.jpg")
        with mock.patch.dict(
            sys.modules,
            {"PIL": pil_module, "PIL.Image": image_module},
        ):
            save_counterclockwise_rotated_jpeg(FakeRequest(), output_path)

        self.assertEqual(
            calls,
            [
                ("make_image", "main"),
                ("transpose", rotate_90),
                ("save", output_path, {"format": "JPEG", "quality": 95}),
            ],
        )

    def test_production_inspection_uses_memory_image_without_disk_write(self) -> None:
        image_marker = object()
        expected_result = InspectionResult(
            command="INNER",
            overall_label="OK",
            left=SidePrediction("OK", 0.99),
            right=SidePrediction("OK", 0.98),
            elapsed_ms=10.0,
        )

        class FakeRequest:
            def __init__(self):
                self.released = False

            def get_metadata(self):
                return {"SensorTimestamp": 123}

            def release(self):
                self.released = True

        class FakeCamera:
            def __init__(self):
                self.request = FakeRequest()

            def capture_request(self, flush):
                self.flush = flush
                return self.request

        class FakeInspectionEngine:
            def __init__(self):
                self.calls = []

            def inspect(self, command, image):
                self.calls.append((command, image))
                return expected_result

        camera = FakeCamera()
        engine = FakeInspectionEngine()
        worker = CaptureWorker(warmup_seconds=0.0)
        results = []
        worker.inspection_completed.connect(results.append)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "not-saved.jpg"
            with mock.patch(
                "main.counterclockwise_rotated_image",
                return_value=image_marker,
            ):
                worker.capture_one(
                    camera,
                    output_path,
                    save_image=False,
                    inspection_kind="INNER",
                    inspection_engine=engine,
                )
            self.assertFalse(output_path.exists())

        self.assertEqual(engine.calls, [("INNER", image_marker)])
        self.assertEqual(results, [expected_result])
        self.assertTrue(camera.request.released)

    def test_inspection_bypass_skips_ai_and_emits_forced_ok(self) -> None:
        class FakeRequest:
            def __init__(self):
                self.released = False

            def get_metadata(self):
                return {"SensorTimestamp": 123}

            def release(self):
                self.released = True

        class FakeCamera:
            def __init__(self):
                self.request = FakeRequest()

            def capture_request(self, flush):
                self.flush = flush
                return self.request

        class FailingInspectionEngine:
            def inspect(self, _command, _image):
                raise AssertionError("AI must not run while bypass is enabled")

        camera = FakeCamera()
        worker = CaptureWorker(warmup_seconds=0.0)
        results = []
        worker.inspection_completed.connect(results.append)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "not-saved.jpg"
            worker.capture_one(
                camera,
                output_path,
                save_image=False,
                inspection_kind="INNER",
                inspection_engine=FailingInspectionEngine(),
                bypass_inspection=True,
            )
            self.assertFalse(output_path.exists())

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].overall_label, "OK")
        self.assertTrue(results[0].was_bypassed)
        self.assertTrue(camera.request.released)

    def test_vt6_commands_use_save_choice_and_return_results(self) -> None:
        controller = CameraController()
        controller.ready = True
        captured_jobs = []

        inspection_kinds = []
        inspection_bypasses = []

        def capture(
            output_path,
            save_image=True,
            inspection_kind=None,
            bypass_inspection=False,
        ):
            controller.busy = True
            captured_jobs.append((output_path, save_image))
            inspection_kinds.append(inspection_kind)
            inspection_bypasses.append(bypass_inspection)
            return True

        controller.capture = capture
        calibration = {
            "PickNP": {"X": 0.05, "Y": -0.10, "Z": 0.0, "U": 0.05},
            "PickNPS": {"X": 0.0, "Y": 0.0, "Z": 0.05, "U": -0.05},
            "DropNP": {"X": -0.10, "Y": 0.0, "Z": 0.0, "U": 0.10},
        }
        save_production_images = [True]
        inspection_bypass = [False]
        server = Vt6TrainingServer(
            controller,
            lambda: calibration,
            port=0,
            save_production_images_provider=(
                lambda: save_production_images[0]
            ),
            inspection_bypass_provider=(lambda: inspection_bypass[0]),
        )
        self.assertTrue(server.start())

        client = QtNetwork.QTcpSocket()
        client.connectToHost("127.0.0.1", server.port)
        self.assertTrue(client.waitForConnected(1000))
        self.assertTrue(self.wait_until(lambda: server.current_client is not None))

        client.write(b"CALIB\r\n")
        client.flush()
        self.assertEqual(
            self.read_response(client),
            "+0.05,-0.10,+0.00,+0.05,+0.00,+0.00,+0.05,-0.05,"
            "-0.10,+0.00,+0.00,+0.10",
        )

        client.write(b"INNER\r\nGLUE\r\nNP\r\n")
        client.flush()
        self.assertTrue(self.wait_until(lambda: len(captured_jobs) == 1))
        self.assertEqual(captured_jobs[0][0].parent.name, "INNER")
        self.assertTrue(captured_jobs[0][1])
        self.assertEqual(inspection_kinds[0], "INNER")
        self.assertFalse(inspection_bypasses[0])

        controller.on_capture_succeeded(str(captured_jobs[0][0]), True)
        self.assertEqual(self.read_response(client), "INNER,OK")
        self.assertTrue(self.wait_until(lambda: len(captured_jobs) == 2))
        self.assertEqual(captured_jobs[1][0].parent.name, "GLUE")
        self.assertTrue(captured_jobs[1][1])
        self.assertEqual(inspection_kinds[1], "GLUE")

        controller.on_capture_succeeded(str(captured_jobs[1][0]), True)
        self.assertEqual(self.read_response(client), "GLUE,OK")
        self.assertTrue(self.wait_until(lambda: len(captured_jobs) == 3))
        self.assertEqual(captured_jobs[2][0].parent.name, "NP")
        self.assertTrue(captured_jobs[2][1])
        self.assertIsNone(inspection_kinds[2])

        controller.on_capture_succeeded(str(captured_jobs[2][0]), True)
        self.assertEqual(self.read_response(client), "NP,OK")

        save_production_images[0] = False
        inspection_bypass[0] = True
        client.write(b"INNER\r\n")
        client.flush()
        self.assertTrue(self.wait_until(lambda: len(captured_jobs) == 4))
        self.assertFalse(captured_jobs[3][1])
        self.assertEqual(inspection_kinds[3], "INNER")
        self.assertTrue(inspection_bypasses[3])
        client.disconnectFromHost()
        self.assertTrue(self.wait_until(lambda: server.current_client is None))

        reconnected_client = QtNetwork.QTcpSocket()
        reconnected_client.connectToHost("127.0.0.1", server.port)
        self.assertTrue(reconnected_client.waitForConnected(1000))
        self.assertTrue(
            self.wait_until(lambda: server.current_client is not None)
        )

        # The old exposure may finish after a new production connection is
        # established. Its result must not be delivered to the new session.
        controller.on_capture_succeeded(str(captured_jobs[3][0]), False)
        self.assertFalse(reconnected_client.waitForReadyRead(100))

        reconnected_client.disconnectFromHost()
        server.stop()

    def test_unknown_command_is_logged_and_queues_fault_photo_without_reply(self) -> None:
        controller = CameraController()
        controller.ready = True
        captured_jobs = []

        def capture(
            output_path,
            save_image=True,
            inspection_kind=None,
            bypass_inspection=False,
        ):
            controller.busy = True
            captured_jobs.append((output_path, save_image))
            return True

        controller.capture = capture

        with tempfile.TemporaryDirectory() as temporary_directory:
            error_root = Path(temporary_directory) / "error_records"
            server = Vt6TrainingServer(
                controller,
                lambda: {},
                port=0,
                error_root=error_root,
            )

            server.handle_command("PICK_NP_NO_VAC")

            self.assertEqual(len(captured_jobs), 1)
            captured_path, save_image = captured_jobs[0]
            self.assertTrue(save_image)
            self.assertEqual(captured_path.parent, error_root)
            self.assertIn("PICK_NP_NO_VAC", captured_path.name)

            log_path = error_root / "error.log"
            self.assertTrue(log_path.exists())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("PICK_NP_NO_VAC", log_text)
            self.assertIn("RECEIVED", log_text)
            self.assertIn(captured_path.name, log_text)

            controller.on_capture_succeeded(str(captured_path), True)

    def test_production_commands_force_ok_when_camera_is_unavailable(self) -> None:
        controller = CameraController()
        controller.ready = False
        server = Vt6TrainingServer(controller, lambda: {}, port=0)
        responses = []
        server.send_response = (
            lambda response, response_session=None: responses.append(
                (response, response_session)
            )
            or True
        )

        server.handle_command("INNER", response_session=7)
        server.handle_command("GLUE", response_session=7)

        self.assertEqual(responses, [("INNER,OK", 7), ("GLUE,OK", 7)])

    def test_production_capture_failure_also_forces_ok(self) -> None:
        controller = CameraController()
        server = Vt6TrainingServer(controller, lambda: {}, port=0)
        responses = []
        server.send_response = (
            lambda response, response_session=None: responses.append(
                (response, response_session)
            )
            or True
        )
        server.active_capture = (
            "INNER",
            Path("captures/inner.jpg"),
            None,
            9,
            False,
            False,
        )

        server.capture_failed("simulated camera failure")

        self.assertEqual(responses, [("INNER,OK", 9)])

    @classmethod
    def wait_until(cls, condition, timeout_seconds=1.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            cls.app.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        return condition()

    @classmethod
    def read_response(cls, client):
        if not cls.wait_until(client.canReadLine):
            raise AssertionError("Timed out waiting for TCP response")
        return bytes(client.readLine()).decode("ascii").strip()


if __name__ == "__main__":
    unittest.main()
