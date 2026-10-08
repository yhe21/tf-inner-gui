"""Epson notification boundary and live TCP checks, without production hardware."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5 import QtCore, QtNetwork, QtWidgets

import main
from error_notifications import ErrorMessageCatalog, ErrorNotificationManager
from inspection import InspectionResult, SidePrediction
from main import CameraController, Vt6TrainingServer


class ReceiveClient:
    def __init__(self, data=b""):
        self.data = data
        self.writes = []

    def readAll(self):
        data, self.data = self.data, b""
        return data

    def state(self):
        return QtNetwork.QAbstractSocket.ConnectedState

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        return True

    def disconnectFromHost(self):
        pass

    def deleteLater(self):
        pass


class EpsonNotificationProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.window = QtWidgets.QWidget()
        self.window.resize(1024, 600)
        self.window.show()
        self.manager = ErrorNotificationManager(
            self.window, catalog=ErrorMessageCatalog(override_path=None)
        )
        self.controller = CameraController()
        self.controller.worker = mock.Mock()
        self.controller.on_ready(4056, 3040, True)
        self.server = Vt6TrainingServer(
            self.controller, lambda: {}, port=0, error_root=self.root / "error_records"
        )
        self.server.error_recorded.connect(self.manager.notify, QtCore.Qt.QueuedConnection)
        self.events = []
        self.server.error_recorded.connect(lambda text, written: self.events.append((text, written)))
        self.client = ReceiveClient()
        self.server.current_client = self.client
        self.server.current_session_id = 1
        self.server.client_buffers[self.client] = bytearray()
        self.server.client_sessions[self.client] = 1
        self.path_patch = mock.patch.object(
            main, "build_capture_path", side_effect=lambda category: self.root / (category + ".jpg")
        )
        self.path_patch.start()

    def tearDown(self):
        self.manager.shutdown()
        self.server.stop()
        self.window.close()
        self.server.deleteLater()
        self.controller.deleteLater()
        self.window.deleteLater()
        self.app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.path_patch.stop()
        self.tmp.cleanup()

    def pump_until(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.fail("Timed out waiting for Qt event")

    def finish_capture(self):
        path = self.server.active_capture[1]
        self.controller.on_capture_succeeded(str(path), False)

    def test_log_is_closed_before_signal_and_popup_does_not_wait_for_photo(self):
        observed = []
        def inspect_log(text, written):
            observed.append((self.server.error_root / "error.log").read_text(encoding="utf-8"))
            self.assertTrue(written)
            self.assertIsNone(self.manager.dialog)
        self.server.error_recorded.connect(inspect_log)
        self.server.handle_command("NO_GLUE", 1)
        self.assertIn("\tNO_GLUE\tRECEIVED\t", observed[0])
        self.assertEqual(self.events, [("NO_GLUE", True)])
        self.assertIsNotNone(self.server.active_capture)
        self.assertFalse(self.server.active_capture[1].exists())
        self.app.processEvents()
        self.assertTrue(self.manager.dialog.isVisible())
        self.assertIsNotNone(self.server.active_capture)
        self.assertEqual(self.client.writes, [])

    def test_camera_offline_still_records_and_notifies_once(self):
        self.controller.ready = False
        self.server.handle_command("NO_INNER", 1)
        self.app.processEvents()
        self.assertEqual(self.events, [("NO_INNER", True)])
        self.assertTrue(self.manager.dialog.isVisible())
        log = (self.server.error_root / "error.log").read_text(encoding="utf-8")
        self.assertIn("RECEIVED", log)
        self.assertIn("CAMERA_NOT_READY", log)
        self.assertIsNone(self.server.active_capture)
        self.assertEqual(self.client.writes, [])

    def test_full_capture_queue_still_notifies_once_without_new_capture(self):
        self.server.capture_queue.extend([None] * main.MAX_CAPTURE_QUEUE)
        self.server.handle_command("PICK_PART_NO_VAC", 1)
        self.app.processEvents()
        self.assertEqual(self.events, [("PICK_PART_NO_VAC", True)])
        self.assertTrue(self.manager.dialog.isVisible())
        self.assertIn("CAPTURE_QUEUE_FULL", (self.server.error_root / "error.log").read_text())
        self.controller.worker.enqueue_capture.assert_not_called()

    def test_log_write_failure_is_visible_and_does_not_drop_error(self):
        self.server.error_root.write_text("blocked directory", encoding="utf-8")
        with mock.patch("sys.stderr"):
            self.server.handle_command("NO_GLUE", 1)
        self.app.processEvents()
        self.assertEqual(self.events, [("NO_GLUE", False)])
        warning = self.manager.dialog.findChild(QtWidgets.QLabel, "logWriteWarning")
        self.assertTrue(warning.isVisible())
        self.assertIsNotNone(self.server.active_capture)

    def test_raw_unicode_markup_and_spaces_survive_tcp_while_commands_normalize(self):
        raw = "  Motor <b>fault</b> & \\u6545\\u969c  ".encode().decode("unicode_escape")
        self.controller.ready = False
        self.client.data = raw.encode("utf-8") + b"\r\n glue \r\n  \r\n"
        self.server.read_client(self.client)
        self.app.processEvents()
        self.assertEqual(self.events, [(raw, True)])
        title = self.manager.dialog.findChild(QtWidgets.QLabel, "errorCode")
        self.assertEqual(title.text(), raw)
        self.assertEqual(title.textFormat(), QtCore.Qt.PlainText)
        self.assertEqual(self.client.writes, [b"GLUE,OK\r\n"])

    def test_local_ng_only_replies_and_epson_return_is_the_notification(self):
        for command, error in (("GLUE", "NO_GLUE"), ("INNER", "NO_INNER")):
            with self.subTest(command=command):
                self.server.handle_command(command, 1)
                side = SidePrediction("NG", 0.99)
                self.controller.on_inspection_completed(InspectionResult(command, "NG", side, side, 50.0))
                self.app.processEvents()
                self.assertEqual(self.client.writes[-1], f"{command},NG\r\n".encode())
                self.assertIsNone(self.manager.dialog)
                self.finish_capture()
                self.server.handle_command(error, 1)
                self.app.processEvents()
                self.assertTrue(self.manager.dialog.isVisible())
                self.manager.dialog.confirm_button.click()
                self.finish_capture()
        self.assertEqual(self.events, [("NO_GLUE", True), ("NO_INNER", True)])

    def test_capture_failure_and_manual_result_do_not_add_notifications(self):
        self.server.handle_command("GLUE", 1)
        self.controller.on_capture_failed("Camera frame acquisition timed out")
        side = SidePrediction("NG", 0.99)
        self.controller.on_inspection_completed(InspectionResult("GLUE", "NG", side, side, 50.0))
        self.app.processEvents()
        self.assertEqual(self.events, [])
        self.assertIsNone(self.manager.dialog)
        self.assertEqual(self.client.writes, [b"GLUE,OK\r\n"])

    def test_confirm_does_not_send_response_or_reset_camera(self):
        self.server.handle_command("NO_GLUE", 1)
        self.app.processEvents()
        self.manager.dialog.confirm_button.click()
        self.assertIsNone(self.manager.dialog)
        self.assertEqual(self.client.writes, [])
        self.assertIsNotNone(self.server.active_capture)
        self.controller.worker.stop.assert_not_called()

    def test_real_tcp_continues_while_notification_is_unconfirmed(self):
        self.server.current_client = None
        self.server.client_buffers.clear()
        self.server.client_sessions.clear()
        self.assertTrue(self.server.start())
        client = QtNetwork.QTcpSocket()
        received = bytearray()
        client.readyRead.connect(lambda: received.extend(bytes(client.readAll())))
        try:
            client.connectToHost(QtNetwork.QHostAddress.LocalHost, self.server.port)
            self.pump_until(lambda: self.server.current_client is not None)
            client.write(b"NO_GLUE\r\n")
            client.flush()
            self.pump_until(lambda: self.manager.dialog is not None)
            self.assertTrue(self.manager.dialog.isVisible())
            self.finish_capture()
            client.write(b"GLUE\r\n")
            client.flush()
            self.pump_until(lambda: self.server.active_capture is not None)
            side = SidePrediction("OK", 0.99)
            self.controller.on_inspection_completed(InspectionResult("GLUE", "OK", side, side, 50.0))
            self.pump_until(lambda: b"GLUE,OK\r\n" in received)
            self.assertTrue(self.manager.dialog.isVisible())
            self.finish_capture()
            client.write(b"CALIB\r\n")
            client.flush()
            self.pump_until(lambda: received.count(b"\r\n") == 2)
            self.assertEqual(self.events, [("NO_GLUE", True)])
            self.assertTrue(self.manager.dialog.isVisible())
        finally:
            client.abort()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
