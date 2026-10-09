"""INNER/GLUE scheduling contracts with manually fired timers and sessions."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from frame_capture import FrameRecord, FrameUnavailable
from frame_capture.triggered import TriggerConfig, TriggeredFrameCapture
from frame_capture.tests.fakes import FakeRequest
from frame_capture.tests.test_camera import wait_until


def frame(timestamp, value=7):
    request = FakeRequest(timestamp, value=value)
    return FrameRecord(1, timestamp, request.image, request.metadata)


class ManualTimer:
    def __init__(self, delay, callback, args=()):
        self.delay, self.callback, self.args = delay, callback, args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A real callback may already have been dispatched when cancel occurs.
        # Deliberately invoke cancelled timers to validate the generation guard.
        self.callback(*self.args)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.active = False
        self.closed = False
        self.single_error = None
        self.start_error = None
        self.query_error = None
        self.stop_error = None
        self.single_hook = None
        self.start_hook = None
        self.inner_frame = frame(100, value=11)
        self.glue_frame = frame(140, value=22)

    def capture_one(self):
        self.calls.append(("capture_one",))
        if self.single_hook:
            self.single_hook()
        if self.single_error:
            raise self.single_error
        return self.inner_frame

    def start_continuous(self, max_duration=6.0):
        self.calls.append(("start_continuous", max_duration))
        if self.start_hook:
            self.start_hook()
        if self.start_error:
            raise self.start_error
        self.active = True
        return self

    def get_before(self, timestamp_ns, timeout=2.0, max_age_ns=None):
        self.calls.append(("get_before", timestamp_ns, timeout, max_age_ns))
        if self.query_error:
            raise self.query_error
        if not self.active:
            raise FrameUnavailable("stopped", "automatic deadline expired")
        return self.glue_frame

    def stop_continuous(self, timeout=3.0):
        self.calls.append(("stop_continuous",))
        self.active = False
        if self.stop_error:
            raise self.stop_error

    def close(self, timeout=3.0):
        self.calls.append(("close",))
        self.active = False
        self.closed = True

    def stats(self):
        return {"mode": "continuous" if self.active else "idle",
                "state": "closed" if self.closed else "running" if self.active else "idle",
                "error": None, "cleanup_error": None,
                "current_capture": None, "last_capture": None}


class TriggeredCaptureTests(unittest.TestCase):
    def make_flow(self, config=None):
        session, timers = FakeSession(), []

        def make_timer(delay, callback, args=()):
            timer = ManualTimer(delay, callback, args)
            timers.append(timer)
            return timer

        flow = TriggeredFrameCapture(session, config, timer_factory=make_timer)
        self.addCleanup(flow.close)
        return flow, session, timers

    def test_defaults_match_requested_three_second_delay_six_second_limit(self):
        config = TriggerConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.start_delay, 3.0)
        self.assertEqual(config.max_duration, 6.0)
        self.assertEqual(config.query_timeout, 2.0)

    def test_inner_returns_its_single_before_starting_delay_timer(self):
        flow, session, timers = self.make_flow()

        def during_capture():
            self.assertEqual(timers, [], "delay must begin after single capture returns")
            self.assertFalse(session.active)

        session.single_hook = during_capture
        result = flow.on_inner()
        self.assertIs(result, session.inner_frame)
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, 3.0)
        self.assertTrue(timers[0].started)
        self.assertTrue(timers[0].daemon)
        self.assertFalse(session.active)
        self.assertEqual(flow.stats()["state"], "waiting")

    def test_glue_queries_strict_predecessor_then_stops_capture(self):
        flow, session, timers = self.make_flow(TriggerConfig(query_timeout=0.7))
        flow.on_inner()
        timers[0].fire()
        self.assertTrue(session.active)
        self.assertIn(("start_continuous", 6.0), session.calls)
        result = flow.on_glue(150)
        self.assertIs(result, session.glue_frame)
        self.assertIsNot(result, session.inner_frame)
        self.assertFalse(session.active)
        query_index = session.calls.index(("get_before", 150, 0.7, None))
        self.assertEqual(session.calls[query_index + 1], ("stop_continuous",))

    def test_glue_does_not_return_success_after_late_capture_failure(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        original_stats = session.stats
        session.stats = lambda: {**original_stats(), "error": "late acquisition failure"}
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("late acquisition failure", str(error.exception))
        self.assertFalse(session.active)

    def test_early_glue_cancels_timer_and_never_substitutes_inner_frame(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "not_ready")
        self.assertTrue(timers[0].cancelled)
        timers[0].fire()
        self.assertFalse(session.active)
        self.assertFalse(any(call[0] in ("get_before", "start_continuous") for call in session.calls))
        self.assertEqual(sum(call[0] == "capture_one" for call in session.calls), 1)

    def test_glue_without_inner_has_no_available_cycle(self):
        flow, session, _ = self.make_flow()
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "not_ready")
        self.assertFalse(any(call[0] == "capture_one" for call in session.calls))

    def test_repeated_inner_invalidates_old_timer_and_starts_only_new_cycle(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        flow.on_inner()
        self.assertEqual(len(timers), 2)
        self.assertTrue(timers[0].cancelled)
        timers[0].fire()
        self.assertFalse(session.active)
        timers[1].fire()
        self.assertTrue(session.active)
        self.assertEqual(sum(call[0] == "start_continuous" for call in session.calls), 1)

    def test_repeated_inner_stops_running_cycle_before_next_single(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()

        def during_second_inner():
            self.assertFalse(session.active, "previous continuous capture must already be stopped")

        session.single_hook = during_second_inner
        flow.on_inner()
        self.assertEqual(flow.stats()["state"], "waiting")
        self.assertFalse(session.active)

    def test_duplicate_timer_callback_does_not_open_second_session(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        timers[0].fire()
        self.assertEqual(sum(call[0] == "start_continuous" for call in session.calls), 1)

    def test_glue_query_timeout_still_stops_continuous_capture(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        session.query_error = FrameUnavailable("timeout", "watermark not received")
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "timeout")
        self.assertFalse(session.active)
        self.assertEqual(session.calls[-1], ("stop_continuous",))
        timers[0].fire()
        self.assertFalse(session.active)

    def test_late_glue_after_auto_stop_cannot_use_old_history(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        session.active = False  # Underlying watchdog has already closed camera.
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertIn(error.exception.reason, ("stopped", "expired"))
        self.assertFalse(session.active)
        self.assertEqual(sum(call[0] == "capture_one" for call in session.calls), 1)

    def test_failed_shutdown_does_not_return_a_successful_glue_frame(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        session.stop_error = FrameUnavailable("cleanup_failed", "camera close failed")
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "cleanup_failed")
        self.assertEqual(flow.stats()["state"], "failed")

    def test_query_failure_keeps_cleanup_failure_as_diagnostic_note(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        session.query_error = FrameUnavailable("timeout", "watermark not received")
        session.stop_error = FrameUnavailable("cleanup_failed", "camera close failed")
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "timeout")
        self.assertTrue(any("camera close failed" in note
                            for note in error.exception.__notes__))

    def test_disabled_mode_uses_independent_single_captures_without_timers(self):
        flow, session, timers = self.make_flow(TriggerConfig(enabled=False))
        self.assertIs(flow.on_inner(), session.inner_frame)
        self.assertIs(flow.on_glue(150), session.inner_frame)
        self.assertEqual(timers, [])
        self.assertEqual(sum(call[0] == "capture_one" for call in session.calls), 2)
        self.assertFalse(any(call[0] in ("get_before", "start_continuous") for call in session.calls))

    def test_switch_off_cancels_waiting_cycle_and_stale_callback(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        flow.set_enabled(False)
        self.assertTrue(timers[0].cancelled)
        self.assertFalse(flow.stats()["enabled"])
        timers[0].fire()
        self.assertFalse(session.active)
        self.assertIs(flow.on_glue(150), session.inner_frame)

    def test_switch_off_stops_active_session_and_switch_on_requires_new_inner(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        timers[0].fire()
        flow.set_enabled(False)
        self.assertFalse(session.active)
        flow.set_enabled(True)
        with self.assertRaises(FrameUnavailable) as error:
            flow.on_glue(150)
        self.assertEqual(error.exception.reason, "not_ready")

    def test_single_capture_failure_does_not_schedule_delayed_capture(self):
        flow, session, timers = self.make_flow()
        session.single_error = FrameUnavailable("capture_failed", "single frame failed")
        with self.assertRaises(FrameUnavailable):
            flow.on_inner()
        self.assertEqual(timers, [])
        self.assertEqual(flow.stats()["state"], "failed")
        self.assertIn("single frame failed", flow.stats()["error"])

    def test_delayed_start_failure_is_reported_in_status(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        session.start_error = FrameUnavailable("capture_failed", "delayed start failed")
        timers[0].fire()
        self.assertEqual(flow.stats()["state"], "failed")
        self.assertIn("delayed start failed", flow.stats()["error"])
        self.assertFalse(session.active)

    def test_close_cancels_pending_timer_and_remains_terminal(self):
        flow, session, timers = self.make_flow()
        flow.on_inner()
        flow.close()
        self.assertTrue(timers[0].cancelled)
        timers[0].fire()
        self.assertTrue(session.closed)
        self.assertFalse(session.active)
        self.assertEqual(flow.stats()["state"], "closed")
        with self.assertRaises(RuntimeError):
            flow.on_inner()
        with self.assertRaises(RuntimeError):
            flow.on_glue(150)

    def test_close_racing_dispatched_start_closes_session_after_start(self):
        flow, session, timers = self.make_flow()
        started, allow_start = threading.Event(), threading.Event()

        def slow_start():
            started.set()
            if not allow_start.wait(1):
                raise AssertionError("test did not release delayed start")

        session.start_hook = slow_start
        flow.on_inner()
        with ThreadPoolExecutor(max_workers=2) as pool:
            timer_future = pool.submit(timers[0].fire)
            self.assertTrue(started.wait(1))
            close_future = pool.submit(flow.close)
            allow_start.set()
            timer_future.result(timeout=1)
            close_future.result(timeout=1)
        self.assertFalse(session.active)
        self.assertTrue(session.closed)
        self.assertEqual(flow.stats()["state"], "closed")

    def test_glue_during_startup_cancels_start_and_returns_not_ready(self):
        flow, session, timers = self.make_flow()
        started, allow_start = threading.Event(), threading.Event()

        def slow_start():
            started.set()
            if not allow_start.wait(1):
                raise AssertionError("test did not release delayed start")

        session.start_hook = slow_start
        flow.on_inner()
        with ThreadPoolExecutor(max_workers=2) as pool:
            timer_future = pool.submit(timers[0].fire)
            self.assertTrue(started.wait(1))
            glue_future = pool.submit(flow.on_glue, 150)
            wait_until(lambda: flow.stats()["state"] == "finishing")
            allow_start.set()
            timer_future.result(timeout=1)
            with self.assertRaises(FrameUnavailable) as error:
                glue_future.result(timeout=1)
        self.assertEqual(error.exception.reason, "not_ready")
        self.assertFalse(session.active)
        self.assertFalse(any(call[0] == "get_before" for call in session.calls))


if __name__ == "__main__":
    unittest.main()
