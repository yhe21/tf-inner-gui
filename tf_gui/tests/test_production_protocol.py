"""Single-result production verdict checks without cameras or robot sockets."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PIL import Image  # noqa: E402
from PyQt5 import QtNetwork, QtWidgets  # noqa: E402

import main  # noqa: E402
from inspection import InspectionResult, SidePrediction  # noqa: E402
from main import CameraController, CaptureWorker, Vt6TrainingServer  # noqa: E402


def verdict(command, label="NG", bypassed=False):
    return InspectionResult(
        command, label, SidePrediction(label, 0.99),
        SidePrediction(label, 0.99), 50.0, was_bypassed=bypassed,
    )


class BufferedClient:
    """Connected transport double retaining the real server's session guard."""

    def __init__(self):
        self.writes = []
        self.readyRead = mock.Mock()
        self.disconnected = mock.Mock()
        self.abort = mock.Mock()
        self.disconnectFromHost = mock.Mock()
        self.deleteLater = mock.Mock()

    def state(self):
        return QtNetwork.QAbstractSocket.ConnectedState

    def write(self, data):
        self.writes.append(bytes(data).decode("ascii").strip())
        return len(data)

    def flush(self):
        return True


class ProductionProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.bypass = False
        self.controller = CameraController()
        self.controller.worker = mock.Mock()
        self.controller.on_ready(4056, 3040, True)
        self.server = Vt6TrainingServer(
            self.controller, lambda: {}, port=0,
            error_root=self.root / "error_records",
            inspection_bypass_provider=lambda: self.bypass,
        )
        self.client = BufferedClient()
        self.server.current_client = self.client
        self.server.current_session_id = 7
        self.server.next_session_id = 8
        self.path_number = 0
        path_patch = mock.patch.object(main, "build_capture_path", self.capture_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def capture_path(self, category):
        self.path_number += 1
        return self.root / category / f"capture-{self.path_number}.jpg"

    def start_capture(self, command):
        self.server.handle_command(command, response_session=self.server.current_session_id)
        self.assertIsNotNone(self.server.active_capture)
        self.assertEqual(self.server.active_capture[0], command)
        return self.server.active_capture[1]

    def complete(self, command, label="NG", bypassed=False):
        output_path = self.start_capture(command)
        self.controller.on_inspection_completed(verdict(command, label, bypassed))
        self.controller.on_capture_succeeded(str(output_path), False)

    def replace_client(self):
        new_client = BufferedClient()
        listener = mock.Mock()
        listener.hasPendingConnections.side_effect = [True, False]
        listener.nextPendingConnection.return_value = new_client
        with mock.patch.object(self.server, "server", listener):
            self.server.accept_connection()
        return new_client

    def test_inner_ng_replies_before_image_completion_and_exactly_once(self):
        output_path = self.start_capture("INNER")
        self.controller.latest_inspection_results["INNER"] = verdict("INNER", "OK")

        self.controller.on_inspection_completed(verdict("INNER"))

        self.assertEqual(self.client.writes, ["INNER,NG"])
        self.assertIsNotNone(self.server.active_capture)
        self.controller.on_inspection_completed(verdict("INNER"))
        self.controller.on_capture_succeeded(str(output_path), True)
        self.assertEqual(self.client.writes, ["INNER,NG"])
        self.assertIsNone(self.server.active_capture)

    def test_inner_ok_replies_immediately(self):
        output_path = self.start_capture("INNER")
        self.controller.on_inspection_completed(verdict("INNER", "OK"))
        self.assertEqual(self.client.writes, ["INNER,OK"])
        self.controller.on_capture_succeeded(str(output_path), False)
        self.assertEqual(self.client.writes, ["INNER,OK"])

    def test_glue_first_and_every_subsequent_ng_replies_ng(self):
        for _ in range(5):
            self.complete("GLUE")
        self.assertEqual(self.client.writes, ["GLUE,NG"] * 5)

    def test_glue_verdict_depends_only_on_current_valid_result(self):
        labels = ["NG", "OK", "NG", "OK", "OK", "NG"]
        for label in labels:
            self.complete("GLUE", label)
        self.assertEqual(self.client.writes, [f"GLUE,{label}" for label in labels])

    def test_unrelated_work_and_manual_results_do_not_change_glue_verdict(self):
        self.complete("GLUE")
        self.complete("INNER", "OK")
        self.complete("INNER", "NG")
        np_path = self.start_capture("NP")
        self.controller.on_capture_succeeded(str(np_path), True)
        self.server.handle_command("CALIB", response_session=7)
        self.server.handle_command("NO_GLUE", response_session=7)
        fault_path = self.server.active_capture[1]
        self.controller.on_capture_succeeded(str(fault_path), True)
        before = list(self.client.writes)
        # Manual captures must not produce an unsolicited robot reply.
        self.controller.on_inspection_completed(verdict("GLUE", "OK"))
        self.controller.on_inspection_completed(verdict("GLUE", "NG"))
        self.assertEqual(self.client.writes, before)
        self.complete("GLUE")
        self.assertEqual(self.client.writes[-1], "GLUE,NG")
        self.assertNotIn("NO_GLUE,OK", self.client.writes)

    def test_missing_result_uses_ok_not_cached_ng(self):
        self.complete("GLUE")
        output_path = self.start_capture("GLUE")
        self.controller.latest_inspection_results["GLUE"] = verdict("GLUE")
        self.controller.on_capture_succeeded(str(output_path), True)
        self.assertEqual(self.client.writes[-1], "GLUE,OK")
        self.complete("GLUE")
        self.assertEqual(self.client.writes, ["GLUE,NG", "GLUE,OK", "GLUE,NG"])

    def test_model_failure_uses_ok_without_suppressing_next_valid_ng(self):
        self.complete("GLUE")
        output_path = self.start_capture("GLUE")
        self.controller.on_inspection_failed("GLUE", "Model execution failed")
        self.controller.on_capture_succeeded(str(output_path), True)
        self.assertEqual(self.client.writes[-1], "GLUE,OK")
        self.complete("GLUE")
        self.assertEqual(self.client.writes, ["GLUE,NG", "GLUE,OK", "GLUE,NG"])

    def test_capture_failure_uses_ok_exactly_once(self):
        self.complete("GLUE")
        self.start_capture("GLUE")
        self.controller.on_capture_failed("Camera timed out")
        self.controller.on_capture_failed("Duplicate completion")
        self.assertEqual(self.client.writes, ["GLUE,NG", "GLUE,OK"])

    def test_valid_ng_is_not_overwritten_by_later_image_save_failure(self):
        self.start_capture("GLUE")
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.assertEqual(self.client.writes, ["GLUE,NG"])
        self.controller.on_capture_failed("Disk full while writing JPEG")
        self.assertEqual(self.client.writes, ["GLUE,NG"])
        self.assertIsNone(self.server.active_capture)

    def test_inner_failure_uses_ok_without_suppressing_next_glue_ng(self):
        self.complete("GLUE")
        self.start_capture("INNER")
        self.controller.on_capture_failed("INNER model failure")
        self.assertEqual(self.client.writes[-1], "INNER,OK")
        self.complete("GLUE")
        self.assertEqual(self.client.writes, ["GLUE,NG", "INNER,OK", "GLUE,NG"])

    def test_wrong_command_and_invalid_label_do_not_send_ng(self):
        output_path = self.start_capture("GLUE")
        self.controller.on_inspection_completed(verdict("INNER"))
        self.controller.on_inspection_completed(verdict("GLUE", "ERROR"))
        self.assertEqual(self.client.writes, [])
        self.controller.on_capture_succeeded(str(output_path), False)
        self.assertEqual(self.client.writes, ["GLUE,OK"])

    def test_glue_ng_replies_before_image_completion_and_exactly_once(self):
        output_path = self.start_capture("GLUE")
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.assertEqual(self.client.writes, ["GLUE,NG"])
        self.assertIsNotNone(self.server.active_capture)
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.controller.on_inspection_completed(verdict("GLUE", "OK"))
        self.controller.on_capture_succeeded(str(output_path), False)
        self.assertEqual(self.client.writes, ["GLUE,NG"])
        self.assertIsNone(self.server.active_capture)

    def test_repeated_glue_requests_receive_one_reply_per_capture(self):
        first_path = self.start_capture("GLUE")
        self.server.handle_command("GLUE", response_session=7)
        self.assertEqual(len(self.server.capture_queue), 1)
        self.assertEqual(self.server.active_capture[1], first_path)

        self.controller.on_inspection_completed(verdict("GLUE"))
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.assertEqual(self.client.writes, ["GLUE,NG"])
        self.controller.on_capture_succeeded(str(first_path), False)

        self.assertIsNotNone(self.server.active_capture)
        second_path = self.server.active_capture[1]
        self.assertNotEqual(first_path, second_path)
        self.controller.on_inspection_completed(verdict("GLUE", "OK"))
        self.controller.on_capture_succeeded(str(second_path), False)
        self.assertEqual(self.client.writes, ["GLUE,NG", "GLUE,OK"])
        self.assertIsNone(self.server.active_capture)

    def test_bypass_request_forces_ok_for_both_inspections(self):
        self.bypass = True
        self.complete("GLUE", "NG")
        self.assertEqual(self.client.writes[-1], "GLUE,OK")
        self.complete("INNER", "NG")
        self.assertEqual(self.client.writes[-1], "INNER,OK")
        self.bypass = False
        self.complete("GLUE", "NG")
        self.assertEqual(self.client.writes[-1], "GLUE,NG")

    def test_explicit_bypassed_result_forces_ok_for_both_inspections(self):
        for command in ("GLUE", "INNER"):
            self.complete(command, "NG", bypassed=True)
        self.assertEqual(self.client.writes, ["GLUE,OK", "INNER,OK"])

    def test_camera_unavailable_keeps_ok_fallback_then_valid_ng_replies_ng(self):
        self.controller.on_camera_unavailable("Camera recovering")
        self.server.handle_command("GLUE", response_session=7)
        self.server.handle_command("INNER", response_session=7)
        self.assertEqual(self.client.writes, ["GLUE,OK", "INNER,OK"])
        self.controller.on_ready(4056, 3040, True)
        self.complete("GLUE")
        self.assertEqual(self.client.writes[-1], "GLUE,NG")

    def test_new_connection_ignores_old_active_result_then_first_ng_replies_ng(self):
        old_path = self.start_capture("GLUE")
        new_client = self.replace_client()
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.controller.on_capture_succeeded(str(old_path), True)
        self.assertEqual(new_client.writes, [])
        self.assertEqual(self.client.writes, [])
        self.complete("GLUE")
        self.assertEqual(new_client.writes, ["GLUE,NG"])

    def test_old_session_failure_cannot_reply_to_new_session(self):
        self.start_capture("GLUE")
        new_client = self.replace_client()
        self.controller.on_capture_failed("Late failure from the retired session")
        self.assertEqual(new_client.writes, [])
        self.complete("GLUE")
        self.assertEqual(new_client.writes, ["GLUE,NG"])

    def test_disconnect_or_stop_prevents_pending_result_reply(self):
        for stop_server in (False, True):
            with self.subTest(stop_server=stop_server):
                self.server.current_client = self.client
                self.server.current_session_id = 7
                output_path = self.start_capture("GLUE")
                if stop_server:
                    self.server.stop()
                else:
                    self.server.client_disconnected(self.client)
                self.controller.on_inspection_completed(verdict("GLUE"))
                self.controller.on_capture_succeeded(str(output_path), True)
                self.assertEqual(self.client.writes, [])
                self.assertIsNone(self.server.current_client)
                self.assertIsNone(self.server.current_session_id)

    def test_retired_client_disconnect_does_not_disconnect_current_client(self):
        self.server.client_disconnected(BufferedClient())
        self.assertIs(self.server.current_client, self.client)
        self.assertEqual(self.server.current_session_id, 7)
        self.complete("GLUE")
        self.assertEqual(self.client.writes, ["GLUE,NG"])

    def test_fault_inspection_result_cannot_send_robot_verdict(self):
        self.server.handle_command("NO_GLUE", response_session=7)
        output_path = self.server.active_capture[1]
        self.controller.on_inspection_completed(verdict("GLUE"))
        self.controller.on_capture_succeeded(str(output_path), True)
        self.assertEqual(self.client.writes, [])

    def test_loopback_socket_receives_ng_while_capture_is_still_busy(self):
        # Bind only loopback on an OS-assigned port; never contact a robot.
        self.server.current_client = None
        self.server.current_session_id = None
        self.assertTrue(self.server.server.listen(QtNetwork.QHostAddress.LocalHost, 0))
        self.addCleanup(self.server.stop)
        socket = QtNetwork.QTcpSocket()

        def wait_until(predicate):
            deadline = time.monotonic() + 2.0
            while not predicate() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.001)
            self.assertTrue(predicate(), "Loopback protocol did not complete in time")

        try:
            socket.connectToHost(QtNetwork.QHostAddress.LocalHost, self.server.server.serverPort())
            wait_until(lambda: self.server.current_client is not None)
            socket.write(b"INNER\r\n")
            socket.flush()
            wait_until(lambda: self.server.active_capture is not None)
            output_path = self.server.active_capture[1]
            self.assertEqual(socket.bytesAvailable(), 0)

            self.controller.on_inspection_completed(verdict("INNER"))

            wait_until(lambda: socket.bytesAvailable() > 0)
            self.assertEqual(bytes(socket.readAll()), b"INNER,NG\r\n")
            self.assertTrue(self.controller.busy)
            self.assertIsNotNone(self.server.active_capture)
            self.controller.on_capture_succeeded(str(output_path), True)
            self.app.processEvents()
            self.assertEqual(socket.bytesAvailable(), 0)
            self.assertFalse(self.controller.busy)
        finally:
            socket.abort()
            self.app.processEvents()

    def test_worker_emits_verdict_before_jpeg_failure_and_protocol_replies_once(self):
        for save_all in (False, True):
            with self.subTest(save_all=save_all):
                self.client.writes.clear()
                output_path = self.start_capture("INNER")
                events = []
                request = mock.Mock()
                request.get_metadata.return_value = {"SensorTimestamp": 123456789}
                request.make_image.return_value = Image.new("RGB", (16, 12))
                camera = mock.Mock()
                camera.capture_request.return_value = request
                result = verdict("INNER")

                def inspect(command, image):
                    events.append("inspect")
                    self.assertEqual(command, "INNER")
                    self.assertEqual(image.size, (12, 16))
                    return result

                def failed_save(image, path):
                    events.append("save")
                    self.assertEqual(self.client.writes, ["INNER,NG"])
                    raise OSError("Disk full")

                worker = CaptureWorker()
                worker.inspection_completed.connect(self.controller.on_inspection_completed)
                worker.inspection_completed.connect(lambda _result: events.append("verdict"))
                worker.failed.connect(self.controller.on_capture_failed)
                worker.succeeded.connect(self.controller.on_capture_succeeded)
                engine = mock.Mock()
                engine.inspect.side_effect = inspect

                with mock.patch.object(main, "save_rotated_jpeg", side_effect=failed_save):
                    worker.capture_one(
                        camera, output_path, save_all,
                        inspection_kind="INNER", inspection_engine=engine,
                    )

                self.assertEqual(events, ["inspect", "verdict", "save"])
                self.assertEqual(self.client.writes, ["INNER,NG"])
                self.assertIsNone(self.server.active_capture)
                request.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
