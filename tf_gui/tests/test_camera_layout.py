import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402
from inspection import InspectionResult, SidePrediction  # noqa: E402
from main import CameraController, CameraMonitorDialog, STYLE_FILE  # noqa: E402


class CameraLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # The Windows offscreen plugin has no system fonts until explicitly loaded.
        if os.name == "nt" and not QtGui.QFontDatabase().families():
            font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for filename in ("segoeui.ttf", "segoeuib.ttf"):
                QtGui.QFontDatabase.addApplicationFont(str(font_dir / filename))
            cls.app.setFont(QtGui.QFont("Segoe UI", 9))

    def make_dialog(self):
        controller = CameraController()
        controller.camera_available = True
        controller.ready = True
        dialog = CameraMonitorDialog(controller)
        dialog.setStyleSheet(STYLE_FILE.read_text(encoding="utf-8"))
        dialog.resize(1024, 600)
        dialog.show()
        self.app.processEvents()
        self.addCleanup(dialog.close)
        return controller, dialog

    def test_two_columns_fit_1024_by_600_with_a_tall_image(self):
        controller, dialog = self.make_dialog()
        controller.on_inspection_completed(
            InspectionResult("INNER", "OK", SidePrediction("OK", 0.999),
                             SidePrediction("OK", 0.991), 900.0)
        )
        controller.on_inspection_completed(
            InspectionResult("GLUE", "NG", SidePrediction("NG", 0.982),
                             SidePrediction("OK", 0.975), 450.0)
        )
        self.app.processEvents()
        self.assertEqual(dialog.size(), QtCore.QSize(1024, 600))
        self.assertGreater(dialog.lblCapturedImage.height(), 500)
        self.assertLess(dialog.cameraImageFrame.geometry().right(),
                        dialog.cameraControlPanel.geometry().left())
        for name in ("lblCaptureMode", "lblInspectionStatus", "lblInnerResult",
                     "lblGlueResult", "lblCameraPageStatus", "btnCameraBack",
                     "chkSaveProductionImages", "chkBypassInspection",
                     "btnAutoExposure", "btnManualCapture"):
            child = getattr(dialog, name)
            rect = QtCore.QRect(child.mapTo(dialog, QtCore.QPoint()), child.size())
            self.assertTrue(dialog.rect().contains(rect), name)
        for name in ("btnCameraBack", "chkSaveProductionImages",
                     "chkBypassInspection", "btnAutoExposure", "btnManualCapture"):
            self.assertGreaterEqual(getattr(dialog, name).height(), 44, name)
        self.assertEqual(len(dialog.lblInnerResult.text().splitlines()), 2)
        self.assertEqual(len(dialog.lblGlueResult.text().splitlines()), 2)

    def test_preloaded_preview_resizes_without_cropping_or_overwriting_source(self):
        _controller, dialog = self.make_dialog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.png"
            source = QtGui.QImage(304, 406, QtGui.QImage.Format_RGB32)
            source.fill(QtGui.QColor("steelblue"))
            self.assertTrue(source.save(str(path)))
            original_bytes = path.read_bytes()
            dialog.show_captured_image(path)
            first_size = dialog.lblCapturedImage.pixmap().size()
            for size in (QtCore.QSize(1024, 600), QtCore.QSize(1200, 720)):
                dialog.resize(size)
                self.app.processEvents()
                preview = dialog.lblCapturedImage.pixmap()
                expected = source.size().scaled(
                    dialog.lblCapturedImage.contentsRect().size(),
                    QtCore.Qt.KeepAspectRatio,
                )
                self.assertIsNotNone(preview)
                self.assertFalse(preview.isNull())
                self.assertEqual(preview.size(), expected)
            self.assertNotEqual(dialog.lblCapturedImage.pixmap().size(), first_size)
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertIsNone(dialog.last_capture_path)
            self.assertEqual(dialog._preview_image_path, path)


if __name__ == "__main__":
    unittest.main()
