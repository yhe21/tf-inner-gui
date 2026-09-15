import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from main import (  # noqa: E402
    APP_VERSION, AdjustmentStore, CameraController, CameraMonitorDialog, CameraSettingsStore,
    CaptureSettingsStore, MainWindow,
)


class MainLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        if os.name == "nt" and not QtGui.QFontDatabase().families():
            font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for filename in ("segoeui.ttf", "segoeuib.ttf"):
                QtGui.QFontDatabase.addApplicationFont(str(font_dir / filename))
            cls.app.setFont(QtGui.QFont("Segoe UI", 9))

    def test_title_and_version_fit_1024_by_600(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(CameraController, "start") as start:
            root = Path(directory)
            window = MainWindow(
                store=AdjustmentStore(root / "adjustments.json"),
                capture_settings_store=CaptureSettingsStore(root / "capture.json"),
                camera_settings_store=CameraSettingsStore(root / "camera.json"),
                tcp_enabled=False,
            )
            try:
                window.resize(1024, 600)
                window.show()
                for vt6_text, camera_text in (
                    ("VT6 Disconnected", "Camera Standby"),
                    ("VT6 Connected", "Camera Ready"),
                    ("VT6 waiting on port 5000", "Camera starting..."),
                    ("VT6 connected", "Camera ready 4056x3040 - manual exposure locked"),
                    ("VT6 waiting on port 5000", "Camera ready 4056x3040 - calibration required"),
                ):
                    window.update_vt6_status(vt6_text, False)
                    window.update_camera_status(camera_text, False)
                    self.app.processEvents()
                    self.assertEqual(window.size(), QtCore.QSize(1024, 600))
                    self.assertEqual(window.windowTitle(), f"TF Inspection v{APP_VERSION}")
                    self.assertEqual(window.lblTitle.text(), f"TF Inspection v{APP_VERSION}")
                    self.assertGreaterEqual(window.lblTitle.width(), window.lblTitle.sizeHint().width())
                    title_rect = QtCore.QRect(
                        window.lblTitle.mapTo(window, QtCore.QPoint()), window.lblTitle.size(),
                    )
                    self.assertTrue(window.rect().contains(title_rect))
                start.assert_called_once()
                self.assertIsNone(window.vt6_server)
            finally:
                window.close()

    def test_compact_camera_header_preserves_details_and_not_ready_states(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(CameraController, "start"):
            root = Path(directory)
            window = MainWindow(
                store=AdjustmentStore(root / "adjustments.json"),
                capture_settings_store=CaptureSettingsStore(root / "capture.json"),
                camera_settings_store=CameraSettingsStore(root / "camera.json"),
                tcp_enabled=False,
            )
            controller = window.camera_controller
            dialog = CameraMonitorDialog(controller, window)
            try:
                window.resize(1024, 600)
                window.show()
                window.update_vt6_status("VT6 waiting on port 5000", False)
                for locked in (True, False):
                    with self.subTest(locked=locked):
                        controller.on_ready(4056, 3040, locked)
                        self.app.processEvents()
                        expected = "Camera ready" if locked else "Not calibrated"
                        self.assertEqual(window.lblCameraStatus.text(), f"● {expected}")
                        self.assertEqual(window.lblCameraStatus.property("statusOk"), locked)
                        self.assertEqual(controller.ready, locked)
                        self.assertIn("4056x3040", controller.status_text)
                        self.assertEqual(window.lblCameraStatus.toolTip(), controller.status_text)
                        self.assertEqual(dialog.lblCameraPageStatus.text(), controller.status_text)
                        self.assertGreaterEqual(window.lblCameraStatus.width(),
                                                window.lblCameraStatus.sizeHint().width())

                controller.on_auto_calibration_succeeded({
                    "exposure_time_us": 12000, "analogue_gain": 1.25,
                    "colour_gains": [1.7, 1.4],
                })
                self.assertEqual(window.lblCameraStatus.text(), "● Camera ready")
                self.assertTrue(window.lblCameraStatus.property("statusOk"))
                self.assertTrue(controller.ready)

                controller.on_initialization_failed("Camera unavailable")
                self.assertEqual(window.lblCameraStatus.text(), "● Camera error: Camera unavailable")
                self.assertFalse(window.lblCameraStatus.property("statusOk"))
                window.update_camera_status("Camera starting...", False)
                self.assertEqual(window.lblCameraStatus.text(), "● Camera starting...")
            finally:
                dialog.close()
                window.close()


if __name__ == "__main__":
    unittest.main()
