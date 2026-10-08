"""Epson error notifications, exercised without camera or robot access."""

import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from PyQt5 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from error_notifications import ErrorMessageCatalog, ErrorNotificationManager  # noqa: E402


ERRORS = {
    "NO_GLUE": {
        "description": "GLUE inspection did not pass on one or both sides.",
        "checks": ["Check the glue machine pump.", "Clean the nozzle.", "Purge the glue system."],
    },
    "NO_INNER": {
        "description": "INNER inspection did not pass.",
        "checks": ["Check the gripper.", "Check the fixture."],
    },
    "NO_CHECKS": {"description": "An error without suggested checks.", "checks": []},
}


def write_catalog(path, errors):
    path.write_text(json.dumps({"schema_version": 1, "language": "en", "errors": errors}), encoding="utf-8")


class ErrorMessageCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.defaults = self.root / "default.json"
        self.override = self.root / "override.json"
        write_catalog(self.defaults, ERRORS)

    def catalog(self):
        return ErrorMessageCatalog(self.defaults, self.override)

    def test_known_message_loads_all_independent_checks(self):
        message = self.catalog().resolve("NO_GLUE")
        self.assertEqual(message.code, "NO_GLUE")
        self.assertEqual(message.raw_text, "NO_GLUE")
        self.assertEqual(message.description, ERRORS["NO_GLUE"]["description"])
        self.assertEqual(list(message.checks), ERRORS["NO_GLUE"]["checks"])
        self.assertTrue(message.known)

    def test_local_override_takes_precedence_and_can_remove_an_entry(self):
        write_catalog(self.override, {"NO_GLUE": {"description": "Operator wording", "checks": ["Local check"]}})
        catalog = self.catalog()
        self.assertEqual(catalog.resolve("NO_GLUE").description, "Operator wording")
        self.assertEqual(list(catalog.resolve("NO_GLUE").checks), ["Local check"])
        removed = catalog.resolve("NO_INNER")
        self.assertFalse(removed.known)
        self.assertEqual(removed.raw_text, "NO_INNER")
        self.assertFalse(removed.checks)

    def test_unknown_raw_text_is_not_normalized_or_treated_as_markup(self):
        raw = "  MixedCase <b>FAULT</b> & unexpected detail  "
        message = self.catalog().resolve(raw)
        self.assertFalse(message.known)
        self.assertEqual(message.raw_text, raw)
        self.assertEqual(message.code, raw)
        self.assertFalse(message.description)
        self.assertFalse(message.checks)

    def test_bad_override_does_not_restore_potentially_outdated_default_advice(self):
        self.override.write_text('{"errors": broken', encoding="utf-8")
        catalog = self.catalog()
        message = catalog.resolve("NO_GLUE")
        self.assertFalse(message.known)
        self.assertEqual(message.raw_text, "NO_GLUE")
        self.assertFalse(message.checks)
        self.assertTrue(catalog.load_error)

    def test_missing_and_invalid_default_degrade_to_received_text(self):
        for contents in (None, "not JSON"):
            with self.subTest(contents=contents):
                if self.defaults.exists():
                    self.defaults.unlink()
                if contents is not None:
                    self.defaults.write_text(contents, encoding="utf-8")
                message = self.catalog().resolve("UNLISTED_FAULT")
                self.assertFalse(message.known)
                self.assertEqual(message.raw_text, "UNLISTED_FAULT")
                self.assertFalse(message.description)
                self.assertFalse(message.checks)

    def test_configuration_is_cached_until_explicit_reload(self):
        catalog = self.catalog()
        write_catalog(self.defaults, {"NO_GLUE": {"description": "Reviewed update", "checks": []}})
        self.assertEqual(catalog.resolve("NO_GLUE").description, ERRORS["NO_GLUE"]["description"])
        self.assertTrue(catalog.reload())
        self.assertEqual(catalog.resolve("NO_GLUE").description, "Reviewed update")
        self.assertFalse(catalog.resolve("NO_GLUE").checks)

    def test_invalid_schema_and_check_types_degrade_safely(self):
        documents = (
            {"schema_version": 2, "errors": {}},
            {"schema_version": 1, "errors": []},
            {"schema_version": 1, "errors": {"NO_GLUE": {"checks": "not a list"}}},
            {"schema_version": 1, "errors": {"NO_GLUE": {"checks": [123]}}},
        )
        for document in documents:
            with self.subTest(document=document):
                self.override.write_text(json.dumps(document), encoding="utf-8")
                catalog = self.catalog()
                self.assertTrue(catalog.load_error)
                message = catalog.resolve("NO_GLUE")
                self.assertFalse(message.known)
                self.assertFalse(message.checks)

    def test_shipped_configuration_retains_reviewed_glue_and_inner_checks(self):
        catalog = ErrorMessageCatalog(PROJECT_DIR / "config" / "error_messages.json", self.override)
        glue = catalog.resolve("NO_GLUE")
        inner = catalog.resolve("NO_INNER")
        self.assertTrue(glue.known)
        self.assertEqual(len(glue.checks), 5)
        self.assertIn("Check the glue machine pump.", glue.checks)
        self.assertIn("Check the moleskin on the stamping station.", glue.checks)
        self.assertIn("Check the gripper.", inner.checks)
        self.assertIn("Check the fixture.", inner.checks)


class ErrorNotificationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        if os.name == "nt" and not QtGui.QFontDatabase().families():
            font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for filename in ("segoeui.ttf", "segoeuib.ttf"):
                QtGui.QFontDatabase.addApplicationFont(str(font_dir / filename))
            cls.app.setFont(QtGui.QFont("Segoe UI", 9))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.defaults = self.root / "default.json"
        self.override = self.root / "override.json"
        write_catalog(self.defaults, ERRORS)
        self.catalog = ErrorMessageCatalog(self.defaults, self.override)
        self.parent = QtWidgets.QWidget()
        self.parent.resize(1024, 600)
        self.parent.show()
        self.managers = []
        self.app.processEvents()

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()
        self.parent.close()
        self.parent.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def manager(self, **kwargs):
        manager = ErrorNotificationManager(self.parent, catalog=self.catalog, **kwargs)
        self.managers.append(manager)
        return manager

    def wait_until(self, predicate, timeout_ms=1200):
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(10)
        self.assertTrue(predicate(), "Qt condition was not reached before the deadline")

    def labels(self, dialog, name):
        return dialog.findChildren(QtWidgets.QLabel, name)

    def test_popup_has_no_auto_close_timer_and_remains_until_confirmation(self):
        self.assertNotIn("timeout_ms", inspect.signature(ErrorNotificationManager).parameters)
        manager = self.manager()
        manager.notify("NO_GLUE")
        dialog = manager.dialog
        self.assertFalse(dialog.findChildren(QtCore.QTimer))
        self.assertFalse(self.labels(dialog, "timeoutHint"))
        QtTest.QTest.qWait(180)
        self.assertIs(manager.dialog, dialog)
        self.assertTrue(dialog.isVisible())
        dialog.confirm_button.click()
        self.assertIsNone(manager.dialog)

    def test_known_error_has_code_description_checks_and_one_confirm(self):
        manager = self.manager()
        manager.notify("NO_GLUE")
        dialog = manager.dialog
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.windowModality(), QtCore.Qt.NonModal)
        self.assertEqual([label.text() for label in self.labels(dialog, "errorCode")], ["NO_GLUE"])
        self.assertEqual([label.text() for label in self.labels(dialog, "errorDescription")], [ERRORS["NO_GLUE"]["description"]])
        self.assertEqual([label.text() for label in self.labels(dialog, "checksHeading")], ["Suggestion"])
        checks = self.labels(dialog, "suggestedCheck")
        self.assertEqual(len(checks), 3)
        for label, expected in zip(checks, ERRORS["NO_GLUE"]["checks"]):
            self.assertIn(expected, label.text())
        buttons = dialog.findChildren(QtWidgets.QPushButton)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text(), "Confirm")
        scroll = dialog.findChild(QtWidgets.QScrollArea, "errorScrollArea")
        self.assertIsNotNone(scroll)
        self.assertFalse(scroll.isAncestorOf(buttons[0]))

    def test_all_visible_text_uses_larger_fonts(self):
        manager = self.manager()
        manager.notify("NO_GLUE", log_written=False)
        dialog = manager.dialog
        self.app.processEvents()
        expected_sizes = {
            "errorCode": 34,
            "errorDescription": 28,
            "checksHeading": 26,
            "suggestedCheck": 26,
            "logWriteWarning": 22,
        }
        for name, expected in expected_sizes.items():
            with self.subTest(label=name):
                labels = self.labels(dialog, name)
                self.assertTrue(labels)
                self.assertTrue(all(label.font().pixelSize() == expected for label in labels))
        self.assertEqual(dialog.confirm_button.font().pixelSize(), 28)

    def test_unknown_error_shows_received_text_without_generic_advice(self):
        manager = self.manager()
        raw = "Custom_Error <b>IMPORTANT</b> & details"
        manager.notify(raw)
        dialog = manager.dialog
        title = self.labels(dialog, "errorCode")
        self.assertEqual(len(title), 1)
        self.assertEqual(title[0].text(), raw)
        self.assertEqual(title[0].textFormat(), QtCore.Qt.PlainText)
        self.assertFalse(self.labels(dialog, "errorDescription"))
        self.assertFalse(self.labels(dialog, "checksHeading"))
        self.assertFalse(self.labels(dialog, "suggestedCheck"))

    def test_configured_markup_is_plain_text_and_empty_checks_hide_heading(self):
        write_catalog(self.defaults, {
            "MARKUP": {"description": "<b>Literal description</b>", "checks": ["<i>Literal check</i>"]},
            "NO_CHECKS": ERRORS["NO_CHECKS"],
        })
        self.catalog = ErrorMessageCatalog(self.defaults, self.override)
        manager = self.manager()
        manager.notify("MARKUP")
        labels = self.labels(manager.dialog, "errorDescription") + self.labels(manager.dialog, "suggestedCheck")
        self.assertEqual(len(labels), 2)
        self.assertTrue(all(label.textFormat() == QtCore.Qt.PlainText for label in labels))
        self.assertIn("<b>Literal description</b>", [label.text() for label in labels])
        manager.dialog.findChild(QtWidgets.QPushButton, "confirmButton").click()
        manager.notify("NO_CHECKS")
        self.assertFalse(self.labels(manager.dialog, "checksHeading"))
        self.assertFalse(self.labels(manager.dialog, "suggestedCheck"))

    def test_background_event_loop_runs_while_popup_waits_for_confirmation(self):
        manager = self.manager()
        ticks = []
        timer = QtCore.QTimer(self.parent)
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        manager.notify("NO_GLUE")
        QtTest.QTest.qWait(100)
        self.assertGreaterEqual(len(ticks), 3)
        self.assertTrue(manager.dialog.isVisible())
        self.assertIsNone(self.app.activeModalWidget())
        timer.stop()

    def test_distinct_error_replaces_previous_message_in_same_window(self):
        manager = self.manager()
        manager.notify("NO_GLUE")
        dialog = manager.dialog
        manager.notify("NO_INNER")
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIs(manager.dialog, dialog)
        self.assertEqual(dialog.message.code, "NO_INNER")
        self.assertEqual([label.text() for label in self.labels(dialog, "errorCode")], ["NO_INNER"])
        self.assertEqual([label.text() for label in self.labels(dialog, "errorDescription")],
                         [ERRORS["NO_INNER"]["description"]])
        self.assertEqual([label.text() for label in self.labels(dialog, "suggestedCheck")],
                         [f"{i}. {check}" for i, check in enumerate(ERRORS["NO_INNER"]["checks"], 1)])
        self.assertFalse(self.labels(dialog, "occurrenceCount"))
        self.assertEqual(len(dialog.findChildren(QtWidgets.QPushButton)), 1)

    def test_same_code_replaces_content_and_does_not_accumulate_occurrences(self):
        manager = self.manager()
        manager.notify("NO_GLUE", log_written=False)
        dialog = manager.dialog
        write_catalog(self.defaults, {"NO_GLUE": {
            "description": "Latest description", "checks": ["Latest suggestion"],
        }})
        self.assertTrue(self.catalog.reload())
        manager.notify("NO_GLUE", log_written=True)
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIs(manager.dialog, dialog)
        self.assertEqual(dialog.message.description, "Latest description")
        self.assertEqual([label.text() for label in self.labels(dialog, "errorDescription")],
                         ["Latest description"])
        self.assertEqual([label.text() for label in self.labels(dialog, "suggestedCheck")],
                         ["1. Latest suggestion"])
        self.assertEqual(len(self.labels(dialog, "errorCode")), 1)
        self.assertFalse(self.labels(dialog, "occurrenceCount"))
        self.assertFalse(any(label.isVisible() for label in self.labels(dialog, "logWriteWarning")))

    def test_unknown_replacement_clears_previous_description_suggestions_and_warning(self):
        manager = self.manager()
        manager.notify("NO_GLUE", log_written=False)
        dialog = manager.dialog
        raw = "Custom_Error <b>IMPORTANT</b> & latest details"
        manager.notify(raw, log_written=True)
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIs(manager.dialog, dialog)
        self.assertEqual(dialog.message.raw_text, raw)
        titles = self.labels(dialog, "errorCode")
        self.assertEqual([label.text() for label in titles], [raw])
        self.assertEqual(titles[0].textFormat(), QtCore.Qt.PlainText)
        self.assertFalse(self.labels(dialog, "errorDescription"))
        self.assertFalse(self.labels(dialog, "checksHeading"))
        self.assertFalse(self.labels(dialog, "suggestedCheck"))
        self.assertFalse(any(label.isVisible() for label in self.labels(dialog, "logWriteWarning")))

    def test_replacement_resets_scrolled_content_to_top(self):
        long_errors = {code: {
            "description": f"{code} details. " * 25,
            "checks": [f"Check {i}: " + "A longer operator instruction. " * 8 for i in range(12)],
        } for code in ("LONG_FIRST", "LONG_SECOND")}
        write_catalog(self.defaults, long_errors)
        self.assertTrue(self.catalog.reload())
        manager = self.manager()
        manager.notify("LONG_FIRST")
        dialog = manager.dialog
        self.app.processEvents()
        scrollbar = dialog.scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(scrollbar.maximum())
        self.assertGreater(scrollbar.value(), 0)
        manager.notify("LONG_SECOND")
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIs(manager.dialog, dialog)
        self.assertEqual(dialog.message.code, "LONG_SECOND")
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), 0)
        self.assertEqual([label.text() for label in self.labels(dialog, "errorCode")], ["LONG_SECOND"])

    def test_confirmation_cleans_state_and_next_error_opens_fresh_window(self):
        manager = self.manager()
        manager.notify("NO_GLUE")
        first = manager.dialog
        first.findChild(QtWidgets.QPushButton, "confirmButton").click()
        self.wait_until(lambda: manager.dialog is None)
        manager.notify("NO_GLUE")
        self.assertIsNot(manager.dialog, first)
        self.assertEqual(manager.dialog.message.code, "NO_GLUE")
        self.assertEqual(len(self.labels(manager.dialog, "errorCode")), 1)
        self.assertTrue(manager.dialog.isVisible())

    def test_failed_log_write_still_displays_fault_and_warning(self):
        manager = self.manager()
        manager.notify("NO_GLUE", log_written=False)
        self.assertTrue(manager.dialog.isVisible())
        warnings = self.labels(manager.dialog, "logWriteWarning")
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].text().strip())

    def test_long_content_stays_scrollable_and_confirm_fits_small_screens(self):
        long_code = "UNKNOWN_" + "X" * 260
        write_catalog(self.defaults, {long_code: {
            "description": "Long explanatory text. " * 40,
            "checks": [f"Check {i}: " + "A longer operator instruction. " * 8 for i in range(20)],
        }})
        self.catalog = ErrorMessageCatalog(self.defaults, self.override)
        for width, height in ((1024, 600), (800, 480)):
            with self.subTest(size=(width, height)):
                self.parent.resize(width, height)
                self.app.processEvents()
                manager = self.manager()
                manager.notify(long_code)
                dialog = manager.dialog
                self.app.processEvents()
                self.assertLessEqual(dialog.width(), width)
                self.assertLessEqual(dialog.height(), height)
                button = dialog.findChild(QtWidgets.QPushButton, "confirmButton")
                button_rect = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint()), button.size())
                self.assertTrue(dialog.rect().contains(button_rect))
                self.assertTrue(button.isVisible())
                self.assertGreaterEqual(button.height(), 56)
                scroll = dialog.findChild(QtWidgets.QScrollArea, "errorScrollArea")
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                manager.shutdown()

    def test_existing_modal_page_keeps_popup_interactive_and_reparents_on_exit(self):
        modal = QtWidgets.QDialog(self.parent)
        modal.setModal(True)
        modal.resize(800, 480)
        modal.show()
        self.app.processEvents()
        manager = self.manager()
        try:
            manager.notify("NO_GLUE")
            self.app.processEvents()
            dialog = manager.dialog
            self.assertIs(dialog.parentWidget(), modal)
            self.assertFalse(dialog.isModal())
            self.assertIs(self.app.activeModalWidget(), modal)
            self.assertTrue(dialog.findChild(QtWidgets.QPushButton, "confirmButton").isEnabled())
            modal.close()
            self.wait_until(lambda: manager.dialog is dialog and dialog.parentWidget() is self.parent)
            self.assertTrue(dialog.isVisible())
            self.assertIsNone(self.app.activeModalWidget())
            dialog.findChild(QtWidgets.QPushButton, "confirmButton").click()
            self.assertIsNone(manager.dialog)
        finally:
            modal.close()
            modal.deleteLater()

    def test_queued_error_in_modal_exec_accepts_real_confirm_input(self):
        class ErrorSource(QtCore.QObject):
            received = QtCore.pyqtSignal(str, bool)

        manager = self.manager()
        source = ErrorSource(self.parent)
        source.received.connect(manager.notify, QtCore.Qt.QueuedConnection)
        modal = QtWidgets.QDialog(self.parent)
        modal.resize(800, 480)
        ticks = []
        observed = {}
        callback_errors = []
        heartbeat = QtCore.QTimer(self.parent)
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
        watchdog = QtCore.QTimer(self.parent)
        watchdog.setSingleShot(True)

        def fail_safe():
            observed["watchdog_fired"] = True
            modal.reject()

        def confirm_with_mouse():
            try:
                dialog = manager.dialog
                observed["business_modal_before"] = self.app.activeModalWidget() is modal
                observed["popup_parent_is_modal"] = dialog.parentWidget() is modal
                observed["ticks_before_confirm"] = len(ticks)
                button = dialog.findChild(QtWidgets.QPushButton, "confirmButton")
                QtTest.QTest.mouseClick(button, QtCore.Qt.LeftButton)
                observed["popup_closed"] = manager.dialog is None
                observed["business_modal_after"] = self.app.activeModalWidget() is modal
                observed["business_still_visible"] = modal.isVisible()
            except Exception as error:
                callback_errors.append(error)
            finally:
                QtCore.QTimer.singleShot(30, modal.reject)

        watchdog.timeout.connect(fail_safe)
        heartbeat.start()
        watchdog.start(1500)
        QtCore.QTimer.singleShot(20, lambda: source.received.emit("NO_GLUE", True))
        QtCore.QTimer.singleShot(100, confirm_with_mouse)
        try:
            result = modal.exec_()
            self.assertEqual(result, QtWidgets.QDialog.Rejected)
            self.assertFalse(callback_errors, repr(callback_errors))
            self.assertNotIn("watchdog_fired", observed)
            self.assertTrue(observed["business_modal_before"])
            self.assertTrue(observed["popup_parent_is_modal"])
            self.assertGreaterEqual(observed["ticks_before_confirm"], 3)
            self.assertTrue(observed["popup_closed"])
            self.assertTrue(observed["business_modal_after"])
            self.assertTrue(observed["business_still_visible"])
            self.assertGreater(len(ticks), observed["ticks_before_confirm"])
        finally:
            heartbeat.stop()
            watchdog.stop()
            modal.close()
            modal.deleteLater()

    def test_opening_and_closing_modal_page_preserves_latest_popup_until_confirmation(self):
        manager = self.manager()
        manager.notify("NO_GLUE")
        dialog = manager.dialog
        modal = QtWidgets.QDialog(self.parent)
        modal.setModal(True)
        modal.resize(800, 480)
        try:
            modal.show()
            self.wait_until(lambda: dialog.parentWidget() is modal)
            self.assertIs(manager.dialog, dialog)
            self.assertTrue(dialog.isVisible())
            manager.notify("NO_INNER")
            self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            self.app.processEvents()
            modal.close()
            self.wait_until(lambda: dialog.parentWidget() is self.parent)
            QtTest.QTest.qWait(180)
            self.assertIs(manager.dialog, dialog)
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.message.code, "NO_INNER")
            self.assertFalse(dialog.findChildren(QtCore.QTimer))
            dialog.confirm_button.click()
            self.assertIsNone(manager.dialog)
        finally:
            modal.close()
            modal.deleteLater()

    def test_many_different_faults_keep_only_latest_message_and_single_confirm(self):
        manager = self.manager()
        for index in range(105):
            manager.notify(f"UNLISTED_FAULT_{index}")
        dialog = manager.dialog
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(dialog.message.raw_text, "UNLISTED_FAULT_104")
        self.assertEqual([label.text() for label in self.labels(dialog, "errorCode")],
                         ["UNLISTED_FAULT_104"])
        self.assertEqual(len(dialog.findChildren(QtWidgets.QPushButton)), 1)
        self.assertFalse(self.labels(dialog, "omittedErrors"))
        self.assertFalse(self.labels(dialog, "occurrenceCount"))

    def test_deleting_modal_page_preserves_popup_until_confirmation(self):
        modal = QtWidgets.QDialog(self.parent)
        modal.setModal(True)
        modal.show()
        self.app.processEvents()
        manager = self.manager()
        manager.notify("NO_GLUE")
        dialog = manager.dialog
        self.assertIs(dialog.parentWidget(), modal)
        modal.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertIs(manager.dialog, dialog)
        self.assertIs(dialog.parentWidget(), self.parent)
        self.assertTrue(dialog.isVisible())
        dialog.findChild(QtWidgets.QPushButton, "confirmButton").click()
        self.assertIsNone(manager.dialog)

    def test_shutdown_closes_popup_and_clears_manager_reference(self):
        manager = self.manager()
        manager.notify("NO_GLUE")
        manager.shutdown()
        self.app.processEvents()
        self.assertIsNone(manager.dialog)
        manager.shutdown()
        manager.notify("NO_INNER")
        self.assertIsNone(manager.dialog)


if __name__ == "__main__":
    unittest.main()
