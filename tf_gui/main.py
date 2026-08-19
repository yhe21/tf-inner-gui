import argparse
import json
import sys
import time
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, uic


APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui" / "main_menu.ui"
ADJUSTMENT_UI_FILE = APP_DIR / "ui" / "adjustment_dialog.ui"
CAMERA_MONITOR_UI_FILE = APP_DIR / "ui" / "camera_monitor_dialog.ui"
STYLE_FILE = APP_DIR / "styles" / "app.qss"
CONFIG_FILE = Path.home() / ".config" / "tf_inner" / "adjustments.json"
CAPTURE_ROOT = APP_DIR / "captures"

STATIONS = ("PickNP", "PickNPS", "DropNP")
AXES = ("X", "Y", "Z", "U")
STEP = 0.05
MIN_VALUE = -0.50
MAX_VALUE = 0.50
DEFAULT_ADJUSTMENTS = {
    station: {axis: 0.0 for axis in AXES} for station in STATIONS
}


def build_capture_path(
    captured_at: Optional[datetime] = None, root: Path = CAPTURE_ROOT
) -> Path:
    """Build captures/YYYYMMDD/YYYYMMDD_HHMMSS_mmm.jpg."""
    captured_at = captured_at or datetime.now()
    date_folder = captured_at.strftime("%Y%m%d")
    timestamp = captured_at.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return root / date_folder / f"{timestamp}.jpg"


class CaptureWorker(QtCore.QObject):
    """Capture one full-resolution still without blocking the GUI thread."""

    succeeded = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, output_path: Path) -> None:
        super().__init__()
        self.output_path = output_path

    @QtCore.pyqtSlot()
    def run(self) -> None:
        camera = None
        try:
            from picamera2 import Picamera2

            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            camera = Picamera2()
            full_resolution = camera.sensor_resolution
            configuration = camera.create_still_configuration(
                main={"size": full_resolution, "format": "RGB888"}
            )
            camera.configure(configuration)
            camera.start()
            time.sleep(1.0)
            camera.capture_file(str(self.output_path))
            self.succeeded.emit(str(self.output_path))
        except Exception as error:  # Picamera2 raises several backend exceptions.
            self.failed.emit(str(error))
        finally:
            if camera is not None:
                with suppress(Exception):
                    camera.stop()
                with suppress(Exception):
                    camera.close()


class CameraMonitorDialog(QtWidgets.QDialog):
    """Temporary camera page for manually collecting training images."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        uic.loadUi(str(CAMERA_MONITOR_UI_FILE), self)
        self.capture_thread: Optional[QtCore.QThread] = None
        self.capture_worker: Optional[CaptureWorker] = None
        self.last_capture_path: Optional[Path] = None

        self.btnManualCapture.clicked.connect(self.start_capture)
        self.btnCameraBack.clicked.connect(self.reject)

    def capture_is_running(self) -> bool:
        return self.capture_thread is not None and self.capture_thread.isRunning()

    def start_capture(self) -> None:
        if self.capture_is_running():
            return

        output_path = build_capture_path()
        self.lblCameraPageStatus.setText("正在拍摄，请保持产品静止……")
        self.btnManualCapture.setEnabled(False)
        self.btnCameraBack.setEnabled(False)

        self.capture_thread = QtCore.QThread(self)
        self.capture_worker = CaptureWorker(output_path)
        self.capture_worker.moveToThread(self.capture_thread)
        self.capture_thread.started.connect(self.capture_worker.run)
        self.capture_worker.succeeded.connect(self.capture_succeeded)
        self.capture_worker.failed.connect(self.capture_failed)
        self.capture_worker.succeeded.connect(self.capture_thread.quit)
        self.capture_worker.failed.connect(self.capture_thread.quit)
        self.capture_thread.finished.connect(self.capture_finished)
        self.capture_thread.finished.connect(self.capture_worker.deleteLater)
        self.capture_thread.finished.connect(self.capture_thread.deleteLater)
        self.capture_thread.start()

    @QtCore.pyqtSlot(str)
    def capture_succeeded(self, path_text: str) -> None:
        self.last_capture_path = Path(path_text)
        self.show_captured_image(self.last_capture_path)
        relative_path = self.last_capture_path.relative_to(APP_DIR)
        self.lblCameraPageStatus.setText(f"拍摄成功，已保存：{relative_path}")

    @QtCore.pyqtSlot(str)
    def capture_failed(self, error_message: str) -> None:
        self.lblCameraPageStatus.setText("拍摄失败，请检查摄像头连接")
        QtWidgets.QMessageBox.critical(
            self,
            "拍摄失败",
            f"无法从摄像头获取图片：\n{error_message}",
        )

    @QtCore.pyqtSlot()
    def capture_finished(self) -> None:
        self.btnManualCapture.setEnabled(True)
        self.btnCameraBack.setEnabled(True)
        self.capture_worker = None
        self.capture_thread = None

    def show_captured_image(self, image_path: Path) -> None:
        reader = QtGui.QImageReader(str(image_path))
        target_size = self.lblCapturedImage.size()
        image_size = reader.size()
        if image_size.isValid():
            image_size.scale(target_size, QtCore.Qt.KeepAspectRatio)
            reader.setScaledSize(image_size)
        image = reader.read()
        if image.isNull():
            self.lblCapturedImage.setText("图片已保存，但预览加载失败")
            return

        self.lblCapturedImage.setPixmap(QtGui.QPixmap.fromImage(image))
        self.lblCapturedImage.setText("")

    def reject(self) -> None:
        if self.capture_is_running():
            QtWidgets.QMessageBox.information(
                self,
                "正在拍摄",
                "请等待当前照片保存完成后再返回。",
            )
            return
        super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.capture_is_running():
            event.ignore()
            QtWidgets.QMessageBox.information(
                self,
                "正在拍摄",
                "请等待当前照片保存完成后再关闭窗口。",
            )
            return
        super().closeEvent(event)


def normalize_value(value: object) -> float:
    """Clamp a saved or edited value to the supported 0.05-unit grid."""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = 0.0

    numeric_value = max(MIN_VALUE, min(MAX_VALUE, numeric_value))
    return round(round(numeric_value / STEP) * STEP, 2)


class AdjustmentStore:
    """Read and atomically save the three stations' calibration offsets."""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path

    def load(self) -> Dict[str, Dict[str, float]]:
        data = deepcopy(DEFAULT_ADJUSTMENTS)
        if not self.path.exists():
            self.save(data)
            return data

        try:
            saved_data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return data

        if not isinstance(saved_data, dict):
            return data

        for station in STATIONS:
            saved_station = saved_data.get(station, {})
            if not isinstance(saved_station, dict):
                continue
            for axis in AXES:
                data[station][axis] = normalize_value(saved_station.get(axis, 0.0))
        return data

    def save(self, data: Dict[str, Dict[str, float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = {
            station: {
                axis: normalize_value(data.get(station, {}).get(axis, 0.0))
                for axis in AXES
            }
            for station in STATIONS
        }
        temporary_file = self.path.with_suffix(".tmp")
        temporary_file.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_file.replace(self.path)


class AdjustmentDialog(QtWidgets.QDialog):
    """Touch-friendly offset editor shared by PickNP, PickNPS and DropNP."""

    def __init__(
        self,
        station: str,
        saved_values: Dict[str, float],
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        uic.loadUi(str(ADJUSTMENT_UI_FILE), self)
        self.station = station
        self.current_values = {
            axis: normalize_value(saved_values.get(axis, 0.0)) for axis in AXES
        }

        self.setWindowTitle(f"{station} 调整")
        self.lblDialogTitle.setText(f"{station} 调整")
        self.lblStep.setText("固定步长：0.05 mm / 0.05°")

        for axis in AXES:
            minus_button = getattr(self, f"btn{axis}Minus")
            plus_button = getattr(self, f"btn{axis}Plus")
            minus_button.clicked.connect(
                lambda _checked=False, selected_axis=axis: self.adjust_axis(
                    selected_axis, -STEP
                )
            )
            plus_button.clicked.connect(
                lambda _checked=False, selected_axis=axis: self.adjust_axis(
                    selected_axis, STEP
                )
            )

        self.btnApply.clicked.connect(self.accept)
        self.btnCancel.clicked.connect(self.reject)
        self.refresh_values()

    def adjust_axis(self, axis: str, amount: float) -> None:
        self.current_values[axis] = normalize_value(
            self.current_values[axis] + amount
        )
        self.refresh_values()

    def refresh_values(self) -> None:
        for axis in AXES:
            value = self.current_values[axis]
            unit = "°" if axis == "U" else "mm"
            getattr(self, f"lbl{axis}Value").setText(f"{value:+.2f} {unit}")
            getattr(self, f"btn{axis}Minus").setEnabled(value > MIN_VALUE)
            getattr(self, f"btn{axis}Plus").setEnabled(value < MAX_VALUE)

    def values(self) -> Dict[str, float]:
        return dict(self.current_values)


class MainWindow(QtWidgets.QMainWindow):
    """Touch-friendly entry page for the TF Inner application."""

    def __init__(self, store: Optional[AdjustmentStore] = None) -> None:
        super().__init__()
        uic.loadUi(str(UI_FILE), self)

        if STYLE_FILE.exists():
            self.setStyleSheet(STYLE_FILE.read_text(encoding="utf-8"))

        self.store = store or AdjustmentStore()
        try:
            self.adjustments = self.store.load()
        except OSError as error:
            self.adjustments = deepcopy(DEFAULT_ADJUSTMENTS)
            QtWidgets.QMessageBox.warning(
                self,
                "读取调整数据失败",
                f"无法读取或创建调整数据文件：\n{error}",
            )

        self.btnCameraMonitor.clicked.connect(self.open_camera_monitor)
        self.btnPickNP.clicked.connect(lambda: self.open_adjustment("PickNP"))
        self.btnPickNPS.clicked.connect(lambda: self.open_adjustment("PickNPS"))
        self.btnDropNP.clicked.connect(lambda: self.open_adjustment("DropNP"))
        self.btnExit.clicked.connect(self.confirm_exit)

        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

    def update_clock(self) -> None:
        self.lblClock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    def select_page(self, page_name: str) -> None:
        # The actual pages will replace this message in the next development step.
        self.lblFooter.setText(f"已选择：{page_name}（页面功能待接入）")

    def open_camera_monitor(self) -> None:
        dialog = CameraMonitorDialog(self)
        if self.isFullScreen():
            dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowFullScreen)
        dialog.exec_()
        if dialog.last_capture_path is not None:
            relative_path = dialog.last_capture_path.relative_to(APP_DIR)
            self.lblFooter.setText(f"最近拍摄：{relative_path}")

    def open_adjustment(self, station: str) -> None:
        dialog = AdjustmentDialog(station, self.adjustments[station], self)
        if self.isFullScreen():
            dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowFullScreen)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.lblFooter.setText(f"{station}：已取消，数据未更改")
            return

        previous_values = self.adjustments[station]
        self.adjustments[station] = dialog.values()
        try:
            self.store.save(self.adjustments)
        except OSError as error:
            self.adjustments[station] = previous_values
            QtWidgets.QMessageBox.critical(
                self,
                "保存失败",
                f"调整数据没有保存：\n{error}",
            )
            self.lblFooter.setText(f"{station}：保存失败")
            return

        summary = "  ".join(
            f"{axis} {value:+.2f}" for axis, value in self.adjustments[station].items()
        )
        self.lblFooter.setText(f"{station} 已保存：{summary}")

    def confirm_exit(self) -> None:
        answer = QtWidgets.QMessageBox.question(
            self,
            "退出系统",
            "确定要关闭操作界面吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TF Inner touch-screen GUI")
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Run in full-screen mode on the Raspberry Pi touch screen.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("TF Inner Detection")

    window = MainWindow()
    if args.fullscreen:
        window.showFullScreen()
    else:
        window.show()

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
