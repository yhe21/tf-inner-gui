"""Non-blocking, configurable notifications for errors received from Epson.

The caller must attempt its error-log write before calling ``notify``.  This
module never talks to the robot or camera and does not interpret AI results.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from PyQt5 import QtCore, QtWidgets, sip


DEFAULT_MESSAGES_PATH = Path(__file__).resolve().parent / "config" / "error_messages.json"
OVERRIDE_MESSAGES_PATH = Path.home() / ".config" / "tf_inner" / "error_messages.json"
MAX_CATALOG_BYTES = 1_048_576
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorMessage:
    code: str
    raw_text: str
    description: str = ""
    checks: Tuple[str, ...] = ()
    known: bool = False


class ErrorMessageCatalog:
    """Load a complete local override, or bundled defaults when none exists.

    Entries are cached: receiving a robot message never causes a disk read.
    Call ``reload`` or restart the GUI after editing a configuration file.
    A broken override intentionally falls back to raw robot text, not defaults.
    """

    def __init__(self, default_path=DEFAULT_MESSAGES_PATH,
                 override_path=OVERRIDE_MESSAGES_PATH):
        self.default_path = Path(default_path)
        self.override_path = Path(override_path) if override_path is not None else None
        self.source_path = self.default_path
        self.load_error: Optional[str] = None
        self._messages: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
        self.reload()

    def reload(self) -> bool:
        self._messages = {}
        self.load_error = None
        try:
            # lexists also treats a broken symlink as an override: never silently
            # display bundled advice when an operator's override cannot be read.
            use_override = self.override_path is not None and (
                self.override_path.exists() or self.override_path.is_symlink()
            )
            self.source_path = self.override_path if use_override else self.default_path
            with self.source_path.open("rb") as source:
                raw = source.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES:
                raise ValueError("error-message configuration exceeds 1 MiB")
            document = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(document, dict) or document.get("schema_version") != 1:
                raise ValueError("schema_version must be 1")
            entries = document.get("errors")
            if not isinstance(entries, dict):
                raise ValueError("errors must be an object")
            validated = {}
            for code, entry in entries.items():
                if not isinstance(code, str) or not code.strip() or code != code.strip():
                    raise ValueError("error codes must be nonempty strings without surrounding spaces")
                if not isinstance(entry, dict):
                    raise ValueError(f"{code}: entry must be an object")
                description = entry.get("description", "")
                checks = entry.get("checks", [])
                if not isinstance(description, str):
                    raise ValueError(f"{code}: description must be a string")
                if not isinstance(checks, list) or not all(isinstance(x, str) for x in checks):
                    raise ValueError(f"{code}: checks must be a list of strings")
                if len(checks) > 100:
                    raise ValueError(f"{code}: at most 100 checks are supported")
                validated[code] = (description, tuple(x for x in checks if x.strip()))
            self._messages = validated
            return True
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            self.load_error = str(error)
            _LOG.warning("Error message configuration unavailable (%s): %s", self.source_path, error)
            return False

    def resolve(self, raw_text: str) -> ErrorMessage:
        code = raw_text.strip()
        entry = self._messages.get(code)
        if entry is None:
            return ErrorMessage(code=raw_text, raw_text=raw_text)
        return ErrorMessage(code=code, raw_text=raw_text,
                            description=entry[0], checks=entry[1], known=True)


class ErrorNotificationDialog(QtWidgets.QDialog):
    """Show only the latest error until the operator dismisses the dialog."""

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint)
        self.setObjectName("errorNotificationDialog")
        self.setWindowTitle("Epson error")
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setModal(False)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.message: Optional[ErrorMessage] = None
        self.log_written = True
        self.setStyleSheet("""
            QDialog#errorNotificationDialog { background: white; border: 2px solid #d5dde5; border-radius: 16px; }
            QScrollArea#errorScrollArea, QWidget#errorContent { background: white; border: none; }
            QFrame#errorCard { background: white; border: none; border-bottom: 1px solid #d5dde5; }
            QLabel { color: #172033; background: transparent; border: none; }
            QLabel#errorCode { color: #b42318; font-size: 34px; font-weight: 700; }
            QLabel#errorDescription { font-size: 28px; }
            QLabel#checksHeading { font-size: 26px; font-weight: 700; margin-top: 8px; }
            QLabel#suggestedCheck { font-size: 26px; }
            QLabel#logWriteWarning { font-size: 22px; color: #b42318; font-weight: 600; }
            QPushButton#confirmButton { color: white; background: #2367a8; border: none;
                border-radius: 12px; font-size: 28px; font-weight: 700; min-height: 64px; }
            QPushButton#confirmButton:pressed { background: #174c80; }
            QPushButton#confirmButton:focus { border: 3px solid #8ebce7; }
            QScrollBar:vertical { width: 22px; }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setObjectName("errorScrollArea")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        QtWidgets.QScroller.grabGesture(self.scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        layout.addWidget(self.scroll, 1)
        self.content: Optional[QtWidgets.QWidget] = None
        self.confirm_button = QtWidgets.QPushButton("Confirm", self)
        self.confirm_button.setObjectName("confirmButton")
        self.confirm_button.setMinimumHeight(64)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.accept)
        layout.addWidget(self.confirm_button)
        self.resize(820, 460)

    @staticmethod
    def _label(text: str, name: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(QtCore.Qt.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        return label

    def set_message(self, message: ErrorMessage, log_written: bool = True) -> None:
        # A new report replaces the entire previous message, including advice,
        # log-write status and scroll position. Original events remain in the log.
        self.message = message
        self.log_written = log_written
        QtWidgets.QScroller.scroller(self.scroll.viewport()).stop()
        previous = self.scroll.takeWidget()
        if previous is not None:
            previous.hide()
            previous.deleteLater()
        self.content = QtWidgets.QWidget()
        self.content.setObjectName("errorContent")
        content_layout = QtWidgets.QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(18)
        card = QtWidgets.QFrame(self.content)
        card.setObjectName("errorCard")
        card.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(self._label(message.code, "errorCode"))
        if message.description:
            card_layout.addWidget(self._label(message.description, "errorDescription"))
        if message.checks:
            card_layout.addWidget(self._label("Suggestion", "checksHeading"))
            for number, check in enumerate(message.checks, 1):
                card_layout.addWidget(self._label(f"{number}. {check}", "suggestedCheck"))
        if not log_written:
            card_layout.addWidget(self._label("The error log could not be written.", "logWriteWarning"))
        content_layout.addWidget(card)
        content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        self.scroll.verticalScrollBar().setValue(0)

    def fit_to_parent(self) -> None:
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else QtWidgets.QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 600)
        if parent is not None and parent.isVisible():
            parent_area = QtCore.QRect(parent.mapToGlobal(QtCore.QPoint(0, 0)), parent.size())
            area = available.intersected(parent_area)
            if area.isEmpty():
                area = available
        else:
            area = available
        self.resize(min(820, max(1, area.width() - 24)), min(460, max(1, area.height() - 24)))
        self.move(area.center() - self.rect().center())


class ErrorNotificationManager(QtCore.QObject):
    """Own the popup and follow modal pages without blocking their event loop."""

    def __init__(self, parent: QtWidgets.QWidget, catalog=None):
        super().__init__(parent)
        self._root = parent
        self.catalog = catalog if catalog is not None else ErrorMessageCatalog()
        self.dialog: Optional[ErrorNotificationDialog] = None
        self._stopped = False
        self._reparenting = False
        self._retarget_timer = QtCore.QTimer(self)
        self._retarget_timer.setSingleShot(True)
        self._retarget_timer.timeout.connect(self._retarget)
        self._app = QtWidgets.QApplication.instance()
        if self._app is not None:
            self._app.installEventFilter(self)

    @QtCore.pyqtSlot(str, bool)
    def notify(self, raw_text: str, log_written: bool = True) -> None:
        if self._stopped:
            return
        message = self.catalog.resolve(raw_text)
        if self.dialog is None:
            dialog = ErrorNotificationDialog(self._target_parent())
            self.dialog = dialog
            dialog.finished.connect(lambda result, current=dialog: self._finished(current))
            dialog.set_message(message, log_written)
            dialog.fit_to_parent()
            dialog.show()
            dialog.raise_()
            dialog.confirm_button.setFocus(QtCore.Qt.OtherFocusReason)
        else:
            self.dialog.set_message(message, log_written)
            self._retarget()
            self.dialog.raise_()

    def _target_parent(self):
        modal = QtWidgets.QApplication.activeModalWidget()
        if modal is not None and modal is not self.dialog and not sip.isdeleted(modal):
            return modal
        return self._root

    def _finished(self, dialog) -> None:
        if self.dialog is dialog:
            self.dialog = None

    def _move_to_parent(self, parent) -> None:
        dialog = self.dialog
        if dialog is None or sip.isdeleted(dialog) or dialog.parentWidget() is parent:
            return
        self._reparenting = True
        try:
            dialog.setParent(parent, QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint)
            dialog.setWindowModality(QtCore.Qt.NonModal)
            dialog.fit_to_parent()
            dialog.show()
            dialog.raise_()
        finally:
            self._reparenting = False

    def _retarget(self) -> None:
        if not self._stopped and self.dialog is not None:
            self._move_to_parent(self._target_parent())

    def eventFilter(self, watched, event) -> bool:
        dialog = self.dialog
        if self._stopped or self._reparenting or dialog is None or sip.isdeleted(dialog):
            return False
        if not isinstance(watched, QtWidgets.QWidget) or watched is dialog:
            return False
        kind = event.type()
        if watched is dialog.parentWidget() and watched is not self._root and kind in (
            QtCore.QEvent.Hide, QtCore.QEvent.Close, QtCore.QEvent.DeferredDelete,
        ):
            # Detach synchronously before a page is hidden/deleted.  A later
            # single-shot reattaches to any remaining modal ancestor.
            self._move_to_parent(self._root)
            self._retarget_timer.start(0)
        elif watched.isWindow() and kind in (QtCore.QEvent.Show, QtCore.QEvent.WindowActivate):
            self._retarget_timer.start(0)
        elif watched is dialog.parentWidget() and kind in (QtCore.QEvent.Resize, QtCore.QEvent.Move):
            dialog.fit_to_parent()
        return False

    def shutdown(self) -> None:
        self._stopped = True
        self._retarget_timer.stop()
        if self._app is not None:
            self._app.removeEventFilter(self)
        dialog = self.dialog
        self.dialog = None
        if dialog is not None and not sip.isdeleted(dialog):
            dialog.reject()
