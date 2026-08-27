import argparse
import json
import queue
import sys
import time
from collections import deque
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtNetwork, QtWidgets, uic


APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui" / "main_menu.ui"
ADJUSTMENT_UI_FILE = APP_DIR / "ui" / "adjustment_dialog.ui"
CAMERA_MONITOR_UI_FILE = APP_DIR / "ui" / "camera_monitor_dialog.ui"
STYLE_FILE = APP_DIR / "styles" / "app.qss"
CONFIG_FILE = Path.home() / ".config" / "tf_inner" / "adjustments.json"
CAPTURE_SETTINGS_FILE = (
    Path.home() / ".config" / "tf_inner" / "capture_settings.json"
)
CAPTURE_ROOT = APP_DIR / "captures"
ERROR_RECORD_ROOT = APP_DIR / "error_records"
CAMERA_BUFFER_COUNT = 4
DEFAULT_TCP_PORT = 5000
MAX_COMMAND_BYTES = 64
MAX_CAPTURE_QUEUE = 100
APP_VERSION = "0.2.2"

STATIONS = ("PickNP", "PickNPS", "DropNP")
AXES = ("X", "Y", "Z", "U")
STEP = 0.05
MIN_VALUE = -0.50
MAX_VALUE = 0.50
DEFAULT_ADJUSTMENTS = {
    station: {axis: 0.0 for axis in AXES} for station in STATIONS
}

CaptureCommand = Tuple[Path, bool]
ImageSaver = Callable[[object, Path], None]
# response_session identifies the TCP connection that issued a capture request.
# A result from an old connection must never be delivered to a new connection.
CaptureJob = Tuple[Optional[str], Path, Optional[str], Optional[int], bool]


def build_capture_path(
    captured_at: Optional[datetime] = None,
    root: Path = CAPTURE_ROOT,
    category: Optional[str] = None,
) -> Path:
    """Build captures/YYYYMMDD/YYYYMMDD_HHMMSS_mmm.jpg."""
    captured_at = captured_at or datetime.now()
    date_folder = captured_at.strftime("%Y%m%d")
    timestamp = captured_at.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    output_directory = root / date_folder
    if category:
        output_directory = output_directory / category.upper()
    return output_directory / f"{timestamp}.jpg"


def sanitize_error_code(error_code: str) -> str:
    safe_code = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in error_code.upper()
    ).strip("_")
    return (safe_code or "UNKNOWN_ERROR")[:64]


def build_error_capture_path(
    error_code: str,
    captured_at: Optional[datetime] = None,
    root: Path = ERROR_RECORD_ROOT,
) -> Path:
    """Build one flat error_records/timestamp_ERROR_CODE.jpg path."""
    captured_at = captured_at or datetime.now()
    timestamp = captured_at.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return root / f"{timestamp}_{sanitize_error_code(error_code)}.jpg"


def tcp_port_value(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("TCP port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("TCP port must be between 1 and 65535")
    return port


def create_picamera2() -> object:
    """Import Picamera2 only on the Raspberry Pi where it is installed."""
    from picamera2 import Picamera2

    return Picamera2()


def save_clockwise_rotated_jpeg(request: object, output_path: Path) -> None:
    """Rotate the captured pixels 90 degrees clockwise and encode one JPEG."""
    from PIL import Image

    image = request.make_image("main")
    rotated_image = image.transpose(Image.Transpose.ROTATE_270)
    rotated_image.save(output_path, format="JPEG", quality=95)


class CaptureWorker(QtCore.QObject):
    """Own one continuously running Picamera2 instance in a worker thread."""

    ready = QtCore.pyqtSignal(int, int)
    initialization_failed = QtCore.pyqtSignal(str)
    capture_started = QtCore.pyqtSignal(str, bool)
    frame_acquired = QtCore.pyqtSignal(str, int, bool)
    succeeded = QtCore.pyqtSignal(str, bool)
    failed = QtCore.pyqtSignal(str)
    stopped = QtCore.pyqtSignal()

    def __init__(
        self,
        camera_factory: Callable[[], object] = create_picamera2,
        warmup_seconds: float = 1.0,
        image_saver: ImageSaver = save_clockwise_rotated_jpeg,
    ) -> None:
        super().__init__()
        self.camera_factory = camera_factory
        self.warmup_seconds = warmup_seconds
        self.image_saver = image_saver
        self.commands: "queue.Queue[Optional[CaptureCommand]]" = queue.Queue()

    def request_capture(self, output_path: Path, save_image: bool = True) -> None:
        """Thread-safe: enqueue a capture without touching the camera object."""
        self.commands.put((output_path, save_image))

    def stop(self) -> None:
        """Thread-safe: finish the current capture, then close the camera."""
        self.commands.put(None)

    @QtCore.pyqtSlot()
    def run(self) -> None:
        camera = None
        try:
            camera = self.camera_factory()
            native_resolution = tuple(camera.sensor_resolution)
            configuration = camera.create_still_configuration(
                main={"size": native_resolution, "format": "RGB888"},
                buffer_count=CAMERA_BUFFER_COUNT,
                queue=False,
            )
            camera.configure(configuration)
            camera.start()

            # Auto-exposure and white balance settle once, not on every trigger.
            time.sleep(self.warmup_seconds)
            self.ready.emit(*native_resolution)

            while True:
                command = self.commands.get()
                if command is None:
                    break
                output_path, save_image = command
                self.capture_started.emit(str(output_path), save_image)
                self.capture_one(camera, output_path, save_image)
        except Exception as error:  # Picamera2 raises several backend exceptions.
            self.initialization_failed.emit(str(error))
        finally:
            if camera is not None:
                with suppress(Exception):
                    camera.stop()
                with suppress(Exception):
                    camera.close()
            self.stopped.emit()

    def capture_one(
        self, camera: object, output_path: Path, save_image: bool
    ) -> None:
        request = None
        try:
            if save_image:
                output_path.parent.mkdir(parents=True, exist_ok=True)

            # flush=True guarantees that exposure starts no earlier than trigger time.
            request = camera.capture_request(flush=True)
            metadata = request.get_metadata()
            sensor_timestamp = int(metadata.get("SensorTimestamp", 0))
            self.frame_acquired.emit(
                str(output_path), sensor_timestamp, save_image
            )
            if save_image:
                self.image_saver(request, output_path)
            self.succeeded.emit(str(output_path), save_image)
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            if request is not None:
                with suppress(Exception):
                    request.release()


class CameraController(QtCore.QObject):
    """Application-wide camera service shared by GUI and future TCP triggers."""

    status_changed = QtCore.pyqtSignal(str, bool)
    capture_started = QtCore.pyqtSignal(str, bool)
    frame_acquired = QtCore.pyqtSignal(str, int, bool)
    capture_succeeded = QtCore.pyqtSignal(str, bool)
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
        self.status_text = "Camera starting..."

    def start(self) -> None:
        if self.thread is not None and self.thread.isRunning():
            return

        self.ready = False
        self.busy = False
        self.status_text = "Camera starting..."
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

    def capture(self, output_path: Path, save_image: bool = True) -> bool:
        if not self.ready or self.busy or self.worker is None:
            return False
        self.busy = True
        self.worker.request_capture(output_path, save_image)
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
        self.status_text = f"Camera ready {width}x{height}"
        self.status_changed.emit(self.status_text, True)

    @QtCore.pyqtSlot(str)
    def on_initialization_failed(self, error_message: str) -> None:
        self.ready = False
        self.busy = False
        self.status_text = f"Camera error: {error_message}"
        self.status_changed.emit(self.status_text, False)

    @QtCore.pyqtSlot(str, bool)
    def on_capture_succeeded(self, path_text: str, saved: bool) -> None:
        self.busy = False
        self.capture_succeeded.emit(path_text, saved)

    @QtCore.pyqtSlot(str)
    def on_capture_failed(self, error_message: str) -> None:
        self.busy = False
        self.capture_failed.emit(error_message)

    @QtCore.pyqtSlot()
    def on_thread_finished(self) -> None:
        self.ready = False
        self.busy = False


class Vt6TrainingServer(QtCore.QObject):
    """Non-blocking VT6 trigger, calibration, and fault-record protocol."""

    status_changed = QtCore.pyqtSignal(str, bool)

    def __init__(
        self,
        camera_controller: CameraController,
        calibration_provider: Callable[[], Dict[str, Dict[str, float]]],
        port: int = DEFAULT_TCP_PORT,
        error_root: Path = ERROR_RECORD_ROOT,
        save_production_images_provider: Optional[Callable[[], bool]] = None,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.camera_controller = camera_controller
        self.calibration_provider = calibration_provider
        self.port = port
        self.error_root = error_root
        self.save_production_images_provider = (
            save_production_images_provider or (lambda: True)
        )
        self.server = QtNetwork.QTcpServer(self)
        self.current_client: Optional[QtNetwork.QTcpSocket] = None
        self.client_buffers: Dict[QtNetwork.QTcpSocket, bytearray] = {}
        self.client_sessions: Dict[QtNetwork.QTcpSocket, int] = {}
        self.next_session_id = 1
        self.current_session_id: Optional[int] = None
        self.capture_queue: Deque[CaptureJob] = deque()
        self.active_capture: Optional[CaptureJob] = None

        self.server.newConnection.connect(self.accept_connection)
        self.camera_controller.status_changed.connect(self.camera_status_changed)
        self.camera_controller.capture_succeeded.connect(self.capture_succeeded)
        self.camera_controller.capture_failed.connect(self.capture_failed)

    def start(self) -> bool:
        if not self.server.listen(QtNetwork.QHostAddress.AnyIPv4, self.port):
            self.status_changed.emit(
                f"VT6 port error: {self.server.errorString()}", False
            )
            return False
        self.port = int(self.server.serverPort())
        self.status_changed.emit(f"VT6 waiting on port {self.port}", False)
        return True

    def stop(self) -> None:
        if self.current_client is not None:
            self.current_client.disconnectFromHost()
        for client in list(self.client_buffers):
            client.deleteLater()
        self.client_buffers.clear()
        self.client_sessions.clear()
        self.current_client = None
        self.current_session_id = None
        self.server.close()

    @QtCore.pyqtSlot()
    def accept_connection(self) -> None:
        while self.server.hasPendingConnections():
            new_client = self.server.nextPendingConnection()
            if new_client is None:
                continue

            previous_client = self.current_client
            self.current_client = new_client
            self.client_buffers[new_client] = bytearray()
            session_id = self.next_session_id
            self.next_session_id += 1
            self.client_sessions[new_client] = session_id
            self.current_session_id = session_id
            new_client.readyRead.connect(
                lambda client=new_client: self.read_client(client)
            )
            new_client.disconnected.connect(
                lambda client=new_client: self.client_disconnected(client)
            )

            if previous_client is not None and previous_client is not new_client:
                previous_client.abort()

        self.status_changed.emit("VT6 connected", True)

    def client_disconnected(self, client: QtNetwork.QTcpSocket) -> None:
        self.client_buffers.pop(client, None)
        self.client_sessions.pop(client, None)
        if self.current_client is client:
            self.current_client = None
            self.current_session_id = None
            if self.server.isListening():
                self.status_changed.emit(
                    f"VT6 waiting on port {self.port}", False
                )
        client.deleteLater()

    def read_client(self, client: QtNetwork.QTcpSocket) -> None:
        if client is not self.current_client or client not in self.client_buffers:
            return
        buffer = self.client_buffers[client]
        buffer.extend(bytes(client.readAll()))

        if len(buffer) > MAX_COMMAND_BYTES and b"\n" not in buffer:
            buffer.clear()
            return

        while b"\n" in buffer:
            raw_line, remaining = buffer.split(b"\n", 1)
            buffer[:] = remaining
            command = raw_line.rstrip(b"\r").decode("ascii", errors="ignore")
            self.handle_command(
                command.strip().upper(), self.client_sessions.get(client)
            )

    def handle_command(
        self, command: str, response_session: Optional[int] = None
    ) -> None:
        if not command:
            return
        if command == "CALIB":
            self.send_response(self.format_calibration(), response_session)
        elif command in {"INNER", "GLUE"}:
            self.enqueue_capture(command, response_session)
        else:
            self.enqueue_error_record(command)

    def format_calibration(self) -> str:
        calibration = self.calibration_provider()
        values = []
        for station in STATIONS:
            for axis in AXES:
                value = normalize_value(calibration.get(station, {}).get(axis, 0.0))
                values.append(f"{value:+.2f}")
        return ",".join(values)

    def enqueue_capture(
        self, command: str, response_session: Optional[int] = None
    ) -> None:
        if not self.camera_controller.ready:
            self.send_response(f"{command},NG", response_session)
            return
        if len(self.capture_queue) >= MAX_CAPTURE_QUEUE:
            self.send_response(f"{command},NG", response_session)
            return

        self.capture_queue.append(
            (
                command,
                build_capture_path(category=command),
                None,
                response_session,
                bool(self.save_production_images_provider()),
            )
        )
        self.start_next_capture()

    def enqueue_error_record(self, error_code: str) -> None:
        output_path = build_error_capture_path(error_code, root=self.error_root)
        self.append_error_log(error_code, output_path, "RECEIVED")

        if not self.camera_controller.ready:
            self.append_error_log(error_code, output_path, "CAMERA_NOT_READY")
            return
        if len(self.capture_queue) >= MAX_CAPTURE_QUEUE:
            self.append_error_log(error_code, output_path, "CAPTURE_QUEUE_FULL")
            return

        # Fault images have priority over queued production captures. An active
        # exposure is allowed to finish before this capture starts.
        self.capture_queue.appendleft(
            (None, output_path, error_code, None, True)
        )
        self.start_next_capture()

    def append_error_log(
        self,
        error_code: str,
        output_path: Path,
        status: str,
    ) -> None:
        recorded_at = datetime.now().isoformat(timespec="milliseconds")
        clean_code = error_code.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        clean_status = status.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        try:
            self.error_root.mkdir(parents=True, exist_ok=True)
            with (self.error_root / "error.log").open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"{recorded_at}\t{clean_code}\t{clean_status}\t{output_path.name}\n"
                )
        except OSError as error:
            print(f"Unable to write RPi error log: {error}", file=sys.stderr)

    def start_next_capture(self) -> None:
        if (
            self.active_capture is not None
            or self.camera_controller.busy
            or not self.camera_controller.ready
            or not self.capture_queue
        ):
            return

        job = self.capture_queue.popleft()
        if self.camera_controller.capture(job[1], save_image=job[4]):
            self.active_capture = job
        else:
            self.capture_queue.appendleft(job)

    @QtCore.pyqtSlot(str, bool)
    def camera_status_changed(self, _status_text: str, is_ready: bool) -> None:
        if is_ready:
            self.start_next_capture()

    @QtCore.pyqtSlot(str, bool)
    def capture_succeeded(self, path_text: str, _saved: bool) -> None:
        if self.active_capture is not None and self.active_capture[1] == Path(path_text):
            (
                response_command,
                _path,
                _error_code,
                response_session,
                _save_image,
            ) = self.active_capture
            self.active_capture = None

            if response_command is not None:
                self.send_response(
                    f"{response_command},OK", response_session
                )
        self.start_next_capture()

    @QtCore.pyqtSlot(str)
    def capture_failed(self, error_message: str) -> None:
        if self.active_capture is not None:
            (
                response_command,
                output_path,
                error_code,
                response_session,
                _save_image,
            ) = self.active_capture
            self.active_capture = None
            if response_command is not None:
                self.send_response(
                    f"{response_command},NG", response_session
                )
            elif error_code is not None:
                self.append_error_log(
                    error_code,
                    output_path,
                    f"CAPTURE_FAILED: {error_message}",
                )
        self.start_next_capture()

    def send_response(
        self, response: str, response_session: Optional[int] = None
    ) -> bool:
        """Reply only on the connection that issued the request.

        The VT6 never waits for a reply. If that connection has already gone
        away, the result is intentionally dropped instead of being replayed to
        a later production cycle.
        """
        return self.write_current(response, response_session)

    def write_current(
        self, response: str, response_session: Optional[int] = None
    ) -> bool:
        client = self.current_client
        if (
            client is None
            or client.state() != QtNetwork.QAbstractSocket.ConnectedState
            or (
                response_session is not None
                and response_session != self.current_session_id
            )
        ):
            return False
        client.write((response + "\r\n").encode("ascii"))
        client.flush()
        return True


class CameraMonitorDialog(QtWidgets.QDialog):
    """Camera page backed by the application-wide production camera service."""

    production_save_changed = QtCore.pyqtSignal(bool)

    def __init__(
        self,
        camera_controller: CameraController,
        parent: Optional[QtWidgets.QWidget] = None,
        production_save_enabled: bool = True,
    ) -> None:
        super().__init__(parent)
        uic.loadUi(str(CAMERA_MONITOR_UI_FILE), self)
        self.camera_controller = camera_controller
        self.last_capture_path: Optional[Path] = None

        self.chkSaveProductionImages.setChecked(production_save_enabled)
        self.btnManualCapture.clicked.connect(self.start_capture)
        self.btnCameraBack.clicked.connect(self.reject)
        self.chkSaveProductionImages.toggled.connect(
            self.production_save_toggled
        )

        self.camera_controller.status_changed.connect(self.camera_status_changed)
        self.camera_controller.capture_started.connect(self.capture_started)
        self.camera_controller.frame_acquired.connect(self.frame_acquired)
        self.camera_controller.capture_succeeded.connect(self.capture_succeeded)
        self.camera_controller.capture_failed.connect(self.capture_failed)

        self.lblCameraPageStatus.setText(self.camera_controller.status_text)
        self.refresh_capture_mode()
        self.refresh_buttons()

    @QtCore.pyqtSlot(bool)
    def production_save_toggled(self, enabled: bool) -> None:
        self.refresh_capture_mode()
        state_text = "enabled" if enabled else "disabled"
        self.lblCameraPageStatus.setText(
            f"INNER/GLUE training image saving {state_text}."
        )
        self.production_save_changed.emit(enabled)

    def refresh_capture_mode(self) -> None:
        state_text = "ON" if self.chkSaveProductionImages.isChecked() else "OFF"
        self.lblCaptureMode.setText(
            f"Production saving {state_text} - Native resolution"
        )

    def capture_is_running(self) -> bool:
        return self.camera_controller.busy

    def start_capture(self) -> None:
        if self.capture_is_running():
            return

        output_path = build_capture_path()
        if not self.camera_controller.capture(output_path):
            self.lblCameraPageStatus.setText(
                "Camera is not ready. Please wait and try again."
            )
            self.refresh_buttons()
            return

        self.lblCameraPageStatus.setText(
            "Trigger received. Acquiring a fresh frame..."
        )
        self.refresh_buttons()

    @QtCore.pyqtSlot(str, bool)
    def camera_status_changed(self, status_text: str, is_ready: bool) -> None:
        self.lblCameraPageStatus.setText(status_text)
        self.refresh_buttons()

    @QtCore.pyqtSlot(str, bool)
    def capture_started(self, _path_text: str, save_image: bool) -> None:
        if save_image:
            status = "Exposing and reading image..."
        else:
            status = "Acquiring production frame (not saving)..."
        self.lblCameraPageStatus.setText(status)
        self.refresh_buttons()

    @QtCore.pyqtSlot(str, int, bool)
    def frame_acquired(
        self, _path_text: str, _sensor_timestamp: int, save_image: bool
    ) -> None:
        if save_image:
            status = "Frame acquired. Saving JPG..."
        else:
            status = "Production frame acquired. No file was saved."
        self.lblCameraPageStatus.setText(status)

    @QtCore.pyqtSlot(str, bool)
    def capture_succeeded(self, path_text: str, saved: bool) -> None:
        if not saved:
            self.lblCameraPageStatus.setText(
                "Production frame captured successfully (not saved)."
            )
            self.refresh_buttons()
            return

        self.last_capture_path = Path(path_text)
        self.show_captured_image(self.last_capture_path)
        relative_path = self.last_capture_path.relative_to(APP_DIR)
        self.lblCameraPageStatus.setText(f"Capture saved: {relative_path}")

    @QtCore.pyqtSlot(str)
    def capture_failed(self, error_message: str) -> None:
        self.lblCameraPageStatus.setText(
            "Capture failed. Check the camera connection."
        )
        self.refresh_buttons()
        QtWidgets.QMessageBox.critical(
            self,
            "Capture Failed",
            f"Unable to acquire an image from the camera:\n{error_message}",
        )

    def refresh_buttons(self) -> None:
        self.btnManualCapture.setEnabled(
            self.camera_controller.ready and not self.camera_controller.busy
        )
        self.btnCameraBack.setEnabled(not self.camera_controller.busy)
        self.chkSaveProductionImages.setEnabled(
            not self.camera_controller.busy
        )

    def show_captured_image(self, image_path: Path) -> None:
        reader = QtGui.QImageReader(str(image_path))
        target_size = self.lblCapturedImage.size()
        image_size = reader.size()
        if image_size.isValid():
            image_size.scale(target_size, QtCore.Qt.KeepAspectRatio)
            reader.setScaledSize(image_size)
        image = reader.read()
        if image.isNull():
            self.lblCapturedImage.setText(
                "Image saved, but the preview could not be loaded."
            )
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
                "Capture in Progress",
                "Wait for the current capture to finish before going back.",
            )
            return
        super().reject()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.capture_is_running():
            event.ignore()
            QtWidgets.QMessageBox.information(
                self,
                "Capture in Progress",
                "Wait for the current capture to finish before closing.",
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


class CaptureSettingsStore:
    """Persist whether VT6 INNER/GLUE captures are saved for training."""

    def __init__(self, path: Path = CAPTURE_SETTINGS_FILE) -> None:
        self.path = path

    def load(self) -> bool:
        if not self.path.exists():
            self.save(True)
            return True

        try:
            saved_data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True

        saved_value = saved_data.get("save_training_images", True)
        return saved_value if isinstance(saved_value, bool) else True

    def save(self, enabled: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = self.path.with_suffix(".tmp")
        temporary_file.write_text(
            json.dumps(
                {"save_training_images": bool(enabled)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
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

        self.setWindowTitle(f"{station} Adjustment")
        self.lblDialogTitle.setText(f"{station} Adjustment")
        self.lblStep.setText("Fixed step: 0.05 mm / 0.05 deg")

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

    def __init__(
        self,
        store: Optional[AdjustmentStore] = None,
        capture_settings_store: Optional[CaptureSettingsStore] = None,
        tcp_port: int = DEFAULT_TCP_PORT,
        tcp_enabled: bool = True,
    ) -> None:
        super().__init__()
        uic.loadUi(str(UI_FILE), self)
        self.setWindowTitle(f"TF Inner Inspection System v{APP_VERSION}")
        self.lblTitle.setText(f"TF Inner Inspection v{APP_VERSION}")

        if STYLE_FILE.exists():
            self.setStyleSheet(STYLE_FILE.read_text(encoding="utf-8"))

        self.store = store or AdjustmentStore()
        self.capture_settings_store = (
            capture_settings_store or CaptureSettingsStore()
        )
        try:
            self.adjustments = self.store.load()
        except OSError as error:
            self.adjustments = deepcopy(DEFAULT_ADJUSTMENTS)
            QtWidgets.QMessageBox.warning(
                self,
                "Adjustment Data Error",
                f"Unable to read or create the adjustment file:\n{error}",
            )

        try:
            self.save_training_images = self.capture_settings_store.load()
        except OSError as error:
            self.save_training_images = True
            QtWidgets.QMessageBox.warning(
                self,
                "Capture Settings Error",
                f"Unable to read or create the capture settings file:\n{error}",
            )

        self.btnCameraMonitor.clicked.connect(self.open_camera_monitor)
        self.btnPickNP.clicked.connect(lambda: self.open_adjustment("PickNP"))
        self.btnPickNPS.clicked.connect(lambda: self.open_adjustment("PickNPS"))
        self.btnDropNP.clicked.connect(lambda: self.open_adjustment("DropNP"))
        self.btnExit.clicked.connect(self.confirm_exit)

        self.camera_controller = CameraController(self)
        self.camera_controller.status_changed.connect(self.update_camera_status)
        self.camera_controller.start()

        self.vt6_server: Optional[Vt6TrainingServer] = None
        if tcp_enabled:
            self.vt6_server = Vt6TrainingServer(
                self.camera_controller,
                lambda: self.adjustments,
                port=tcp_port,
                save_production_images_provider=(
                    lambda: self.save_training_images
                ),
                parent=self,
            )
            self.vt6_server.status_changed.connect(self.update_vt6_status)
            self.vt6_server.start()
        else:
            self.update_vt6_status("VT6 service disabled", False)

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

    @QtCore.pyqtSlot(str, bool)
    def update_vt6_status(self, status_text: str, is_connected: bool) -> None:
        self.lblVt6Status.setText(f"● {status_text}")
        self.lblVt6Status.setProperty("statusOk", is_connected)
        self.lblVt6Status.style().unpolish(self.lblVt6Status)
        self.lblVt6Status.style().polish(self.lblVt6Status)

    def select_page(self, page_name: str) -> None:
        # The actual pages will replace this message in the next development step.
        self.lblFooter.setText(f"Selected: {page_name} (page not implemented)")

    def open_camera_monitor(self) -> None:
        dialog = CameraMonitorDialog(
            self.camera_controller,
            self,
            production_save_enabled=self.save_training_images,
        )
        dialog.production_save_changed.connect(
            self.set_production_image_saving
        )
        if self.isFullScreen():
            dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowFullScreen)
        dialog.exec_()
        if dialog.last_capture_path is not None:
            relative_path = dialog.last_capture_path.relative_to(APP_DIR)
            self.lblFooter.setText(f"Latest capture: {relative_path}")

    @QtCore.pyqtSlot(bool)
    def set_production_image_saving(self, enabled: bool) -> None:
        self.save_training_images = enabled
        try:
            self.capture_settings_store.save(enabled)
        except OSError as error:
            QtWidgets.QMessageBox.warning(
                self,
                "Capture Settings Error",
                "The setting is active for this session but could not be "
                f"saved:\n{error}",
            )

    def open_adjustment(self, station: str) -> None:
        dialog = AdjustmentDialog(station, self.adjustments[station], self)
        if self.isFullScreen():
            dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowFullScreen)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.lblFooter.setText(f"{station}: canceled; values unchanged")
            return

        previous_values = self.adjustments[station]
        self.adjustments[station] = dialog.values()
        try:
            self.store.save(self.adjustments)
        except OSError as error:
            self.adjustments[station] = previous_values
            QtWidgets.QMessageBox.critical(
                self,
                "Save Failed",
                f"Adjustment values were not saved:\n{error}",
            )
            self.lblFooter.setText(f"{station}: save failed")
            return

        summary = "  ".join(
            f"{axis} {value:+.2f}" for axis, value in self.adjustments[station].items()
        )
        self.lblFooter.setText(f"{station} saved: {summary}")

    def confirm_exit(self) -> None:
        answer = QtWidgets.QMessageBox.question(
            self,
            "Exit System",
            "Close the operator interface?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.vt6_server is not None:
            self.vt6_server.stop()
        if not self.camera_controller.stop():
            event.ignore()
            QtWidgets.QMessageBox.warning(
                self,
                "Camera Busy",
                "The camera is finishing a task. Try exiting again shortly.",
            )
            return
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TF Inner touch-screen GUI")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {APP_VERSION}"
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Run in full-screen mode on the Raspberry Pi touch screen.",
    )
    parser.add_argument(
        "--tcp-port",
        type=tcp_port_value,
        default=DEFAULT_TCP_PORT,
        metavar="PORT",
        help=f"VT6 TCP port (default: {DEFAULT_TCP_PORT}).",
    )
    parser.add_argument(
        "--no-tcp",
        action="store_true",
        help="Disable the VT6 TCP server for UI-only testing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("TF Inner Detection")

    window = MainWindow(
        tcp_port=args.tcp_port,
        tcp_enabled=not args.no_tcp,
    )
    if args.fullscreen:
        window.showFullScreen()
    else:
        window.show()

    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
