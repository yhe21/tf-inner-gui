import argparse
import json
import queue
import sys
import time
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, uic


APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui" / "main_menu.ui"
ADJUSTMENT_UI_FILE = APP_DIR / "ui" / "adjustment_dialog.ui"
CAMERA_MONITOR_UI_FILE = APP_DIR / "ui" / "camera_monitor_dialog.ui"
STYLE_FILE = APP_DIR / "styles" / "app.qss"
CONFIG_FILE = Path.home() / ".config" / "tf_inner" / "adjustments.json"
CAPTURE_ROOT = APP_DIR / "captures"
PRODUCTION_CAPTURE_SIZE = (1920, 1080)
CAMERA_BUFFER_COUNT = 4

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


def create_picamera2() -> object:
    """Import Picamera2 only on the Raspberry Pi where it is installed."""
    from picamera2 import Picamera2

    return Picamera2()


class CaptureWorker(QtCore.QObject):
    """Own one continuously running Picamera2 instance in a worker thread."""

    ready = QtCore.pyqtSignal(int, int)
    initialization_failed = QtCore.pyqtSignal(str)
    capture_started = QtCore.pyqtSignal(str)
    frame_acquired = QtCore.pyqtSignal(str, int)
    succeeded = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)
    stopped = QtCore.pyqtSignal()

    def __init__(
        self,
        camera_factory: Callable[[], object] = create_picamera2,
        warmup_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        self.camera_factory = camera_factory
        self.warmup_seconds = warmup_seconds
        self.commands: "queue.Queue[Optional[Path]]" = queue.Queue()

    def request_capture(self, output_path: Path) -> None:
        """Thread-safe: enqueue a capture without touching the camera object."""
        self.commands.put(output_path)

    def stop(self) -> None:
        """Thread-safe: finish the current capture, then close the camera."""
        self.commands.put(None)

    @QtCore.pyqtSlot()
    def run(self) -> None:
        camera = None
        try:
            camera = self.camera_factory()
            configuration = camera.create_still_configuration(
                main={"size": PRODUCTION_CAPTURE_SIZE, "format": "RGB888"},
                buffer_count=CAMERA_BUFFER_COUNT,
                queue=False,
            )
            camera.configure(configuration)
            camera.start()

            # Auto-exposure and white balance settle once, not on every trigger.
            time.sleep(self.warmup_seconds)
            self.ready.emit(*PRODUCTION_CAPTURE_SIZE)

            while True:
                output_path = self.commands.get()
                if output_path is None:
                    break
                self.capture_started.emit(str(output_path))
                self.capture_one(camera, output_path)
        except Exception as error:  # Picamera2 raises several backend exceptions.
            self.initialization_failed.emit(str(error))
        finally:
            if camera is not None:
                with suppress(Exception):
                    camera.stop()
                with suppress(Exception):
                    camera.close()
            self.stopped.emit()

    def capture_one(self, camera: object, output_path: Path) -> None:
        request = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # flush=True guarantees that exposure starts no earlier than trigger time.
            request = camera.capture_request(flush=True)
            metadata = request.get_metadata()
            sensor_timestamp = int(metadata.get("SensorTimestamp", 0))
            self.frame_acquired.emit(str(output_path), sensor_timestamp)
            request.save("main", str(output_path))
            self.succeeded.emit(str(output_path))
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if request is not None:
                with suppress(Exception):
                    request.release()


class CameraController(QtCore.QObject):
    """Application-wide camera service shared by GUI and future TCP triggers."""

    status_changed = QtCore.pyqtSignal(str, bool)
    capture_started = QtCore.pyqtSignal(str)
    frame_acquired = QtCore.pyqtSignal(str, int)
    capture_succeeded = QtCore.pyqtSignal(str)
    capture_failed = QtCore.pyqtSignal(str)

    def __init__(
        self,
        parent: Optional[QtCore.QObject] = None,
        worker_factory: Callable[[], CaptureWorker] = CaptureWorker,
    ) -> None:
        super().__init__(parent)
        self.worker_factory = worker_factory
        self.thread: Optional[QtCore.QThread] = None
        self.worker: Optional[CaptureWorker] = None
        self.ready = False
        self.busy = False
        self.status_text = "摄像头正在启动……"

    def start(self) -> None:
        if self.thread is not None and self.thread.isRunning():
            return

        self.ready = False
        self.busy = False
        self.status_text = "摄像头正在启动……"
        self.status_changed.emit(self.status_text, False)

        self.thread = QtCore.QThread(self)
        self.worker = self.worker_factory()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.ready.connect(self.on_ready)
        self.worker.initialization_failed.connect(self.on_initialization_failed)
        self.worker.capture_started.connect(self.capture_started)
        self.worker.frame_acquired.connect(self.frame_acquired)
        self.worker.succeeded.connect(self.on_capture_succeeded)
        self.worker.failed.connect(self.on_capture_failed)
        self.worker.stopped.connect(
            self.thread.quit, type=QtCore.Qt.DirectConnection
        )
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.start()

    def capture(self, output_path: Path) -> bool:
        if not self.ready or self.busy or self.worker is None:
            return False
        self.busy = True
        self.worker.request_capture(output_path)
        return True

    def stop(self, timeout_ms: int = 5000) -> bool:
        if self.thread is None or not self.thread.isRunning():
            return True
        if self.worker is not None:
            self.worker.stop()
        stopped = self.thread.wait(timeout_ms)
        self.ready = False
        self.busy = False
        return stopped

    @QtCore.pyqtSlot(int, int)
    def on_ready(self, width: int, height: int) -> None:
        self.ready = True
        self.status_text = f"Camera 就绪 {width}×{height}"
        self.status_changed.emit(self.status_text, True)

    @QtCore.pyqtSlot(str)
    def on_initialization_failed(self, error_message: str) -> None:
        self.ready = False
        self.busy = False
        self.status_text = f"Camera 错误：{error_message}"
        self.status_changed.emit(self.status_text, False)

    @QtCore.pyqtSlot(str)
    def on_capture_succeeded(self, path_text: str) -> None:
        self.busy = False
        self.capture_succeeded.emit(path_text)

    @QtCore.pyqtSlot(str)
    def on_capture_failed(self, error_message: str) -> None:
        self.busy = False
        self.capture_failed.emit(error_message)

    @QtCore.pyqtSlot()
    def on_thread_finished(self) -> None:
        self.ready = False
        self.busy = False


class CameraMonitorDialog(QtWidgets.QDialog):
    """Camera page backed by the application-wide production camera service."""

    def __init__(
        self,
        camera_controller: CameraController,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        uic.loadUi(str(CAMERA_MONITOR_UI_FILE), self)
        self.camera_controller = camera_controller
        self.last_capture_path: Optional[Path] = None

        self.btnManualCapture.clicked.connect(self.start_capture)
        self.btnCameraBack.clicked.connect(self.reject)

        self.camera_controller.status_changed.connect(self.camera_status_changed)
        self.camera_controller.capture_started.connect(self.capture_started)
        self.camera_controller.frame_acquired.connect(self.frame_acquired)
        self.camera_controller.capture_succeeded.connect(self.capture_succeeded)
        self.camera_controller.capture_failed.connect(self.capture_failed)

        self.lblCameraPageStatus.setText(self.camera_controller.status_text)
        self.refresh_buttons()

    def capture_is_running(self) -> bool:
        return self.camera_controller.busy

    def start_capture(self) -> None:
        if self.capture_is_running():
            return

        output_path = build_capture_path()
        if not self.camera_controller.capture(output_path):
            self.lblCameraPageStatus.setText("摄像头尚未就绪，请稍候再试")
            self.refresh_buttons()
            return

        self.lblCameraPageStatus.setText("已收到触发，正在获取新帧……")
        self.refresh_buttons()

    @QtCore.pyqtSlot(str, bool)
    def camera_status_changed(self, status_text: str, is_ready: bool) -> None:
        self.lblCameraPageStatus.setText(status_text)
        self.refresh_buttons()

    @QtCore.pyqtSlot(str)
    def capture_started(self, _path_text: str) -> None:
        self.lblCameraPageStatus.setText("正在曝光并读取图像……")
        self.refresh_buttons()

    @QtCore.pyqtSlot(str, int)
    def frame_acquired(self, _path_text: str, _sensor_timestamp: int) -> None:
        self.lblCameraPageStatus.setText("图像已采集，正在保存 JPG……")

    @QtCore.pyqtSlot(str)
    def capture_succeeded(self, path_text: str) -> None:
        self.last_capture_path = Path(path_text)
        self.show_captured_image(self.last_capture_path)
        relative_path = self.last_capture_path.relative_to(APP_DIR)
        self.lblCameraPageStatus.setText(f"拍摄成功，已保存：{relative_path}")

    @QtCore.pyqtSlot(str)
    def capture_failed(self, error_message: str) -> None:
        self.lblCameraPageStatus.setText("拍摄失败，请检查摄像头连接")
        self.refresh_buttons()
        QtWidgets.QMessageBox.critical(
            self,
            "拍摄失败",
            f"无法从摄像头获取图片：\n{error_message}",
        )

    def refresh_buttons(self) -> None:
        self.btnManualCapture.setEnabled(
            self.camera_controller.ready and not self.camera_controller.busy
        )
        self.btnCameraBack.setEnabled(not self.camera_controller.busy)

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
        self.refresh_buttons()

    def disconnect_controller(self) -> None:
        connections = (
            (self.camera_controller.status_changed, self.camera_status_changed),
            (self.camera_controller.capture_started, self.capture_started),
            (self.camera_controller.frame_acquired, self.frame_acquired),
            (self.camera_controller.capture_succeeded, self.capture_succeeded),
            (self.camera_controller.capture_failed, self.capture_failed),
        )
        for signal, slot in connections:
            with suppress(TypeError):
                signal.disconnect(slot)

    def done(self, result: int) -> None:
        self.disconnect_controller()
        super().done(result)

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

        self.camera_controller = CameraController(self)
        self.camera_controller.status_changed.connect(self.update_camera_status)
        self.camera_controller.start()

        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

    def update_clock(self) -> None:
        self.lblClock.setText(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))

    @QtCore.pyqtSlot(str, bool)
    def update_camera_status(self, status_text: str, is_ready: bool) -> None:
        self.lblCameraStatus.setText(f"● {status_text}")
        self.lblCameraStatus.setProperty("statusOk", is_ready)
        self.lblCameraStatus.style().unpolish(self.lblCameraStatus)
        self.lblCameraStatus.style().polish(self.lblCameraStatus)

    def select_page(self, page_name: str) -> None:
        # The actual pages will replace this message in the next development step.
        self.lblFooter.setText(f"已选择：{page_name}（页面功能待接入）")

    def open_camera_monitor(self) -> None:
        dialog = CameraMonitorDialog(self.camera_controller, self)
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

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if not self.camera_controller.stop():
            event.ignore()
            QtWidgets.QMessageBox.warning(
                self,
                "摄像头仍在工作",
                "摄像头正在完成当前任务，请稍后再次退出。",
            )
            return
        super().closeEvent(event)


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
