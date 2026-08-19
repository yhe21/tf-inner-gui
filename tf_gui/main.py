import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PyQt5 import QtCore, QtWidgets, uic


APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui" / "main_menu.ui"
ADJUSTMENT_UI_FILE = APP_DIR / "ui" / "adjustment_dialog.ui"
STYLE_FILE = APP_DIR / "styles" / "app.qss"
CONFIG_FILE = Path.home() / ".config" / "tf_inner" / "adjustments.json"

STATIONS = ("PickNP", "PickNPS", "DropNP")
AXES = ("X", "Y", "Z", "U")
STEP = 0.05
MIN_VALUE = -0.50
MAX_VALUE = 0.50
DEFAULT_ADJUSTMENTS = {
    station: {axis: 0.0 for axis in AXES} for station in STATIONS
}


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

        self.btnCameraMonitor.clicked.connect(
            lambda: self.select_page("摄像头结果监测")
        )
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
