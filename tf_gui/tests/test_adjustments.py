import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtWidgets  # noqa: E402

from main import (  # noqa: E402
    AdjustmentDialog,
    AdjustmentStore,
    CameraController,
    CameraMonitorDialog,
    CaptureWorker,
    CAMERA_BUFFER_COUNT,
    MAX_VALUE,
    MIN_VALUE,
    STEP,
    build_capture_path,
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

    def test_camera_page_loads_without_picamera_on_pc(self) -> None:
        controller = CameraController()
        dialog = CameraMonitorDialog(controller)
        self.assertEqual(dialog.btnManualCapture.text(), "手动拍摄并保存")
        self.assertFalse(dialog.capture_is_running())

    def test_persistent_worker_captures_a_fresh_frame_and_releases_it(self) -> None:
        class FakeRequest:
            def __init__(self) -> None:
                self.released = False
                self.saved_stream = None

            def get_metadata(self):
                return {"SensorTimestamp": 123456789}

            def save(self, stream_name, path_text):
                self.saved_stream = stream_name
                Path(path_text).write_bytes(b"fake-jpeg")

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
        worker = CaptureWorker(lambda: fake_camera, warmup_seconds=0.0)
        acquired = []
        succeeded = []
        worker.frame_acquired.connect(
            lambda path, timestamp: acquired.append((path, timestamp))
        )
        worker.succeeded.connect(succeeded.append)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "capture.jpg"
            worker.request_capture(output_path)
            worker.stop()
            worker.run()

            self.assertTrue(output_path.exists())
            self.assertEqual(succeeded, [str(output_path)])
            self.assertEqual(acquired, [(str(output_path), 123456789)])

        options = fake_camera.configuration_options
        self.assertEqual(options["main"]["size"], fake_camera.sensor_resolution)
        self.assertEqual(options["buffer_count"], CAMERA_BUFFER_COUNT)
        self.assertFalse(options["queue"])
        self.assertTrue(fake_camera.flush_value)
        self.assertEqual(fake_camera.request.saved_stream, "main")
        self.assertTrue(fake_camera.request.released)
        self.assertTrue(fake_camera.stopped)
        self.assertTrue(fake_camera.closed)


if __name__ == "__main__":
    unittest.main()
