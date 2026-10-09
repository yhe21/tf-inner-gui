"""Keep-alive sessions pause copying while one camera remains started."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress

from frame_capture import CameraSettings, CaptureConfig, FrameUnavailable
from frame_capture.warm import KeepAliveFrameCapture
from frame_capture.triggered import TriggerConfig, TriggeredFrameCapture
from frame_capture.tests.fakes import FakeCamera, FakeRequest
from frame_capture.tests.test_camera import wait_until
from frame_capture.tests.test_triggered import ManualTimer


STAMP_BASE = 10_000_000  # Preserve the real 5 ms exposure in request metadata.


class CountedRequest(FakeRequest):
    def __init__(self, timestamp_ns, **kwargs):
        super().__init__(STAMP_BASE + timestamp_ns, **kwargs)
        self.copy_count = 0

    def make_array(self, name):
        self.copy_count += 1
        return super().make_array(name)


class TimedJob:
    """A new sensor completion; no producer runs when the app stops reading."""
    def __init__(self, camera, request):
        self.camera, self.request = camera, request
        self.ready = time.monotonic() + camera.interval

    def get_result(self, timeout=None):
        delay = max(0.0, self.ready - time.monotonic())
        if timeout is not None and delay > timeout:
            time.sleep(timeout)
            raise TimeoutError()
        time.sleep(delay)
        return self.request


class StreamingCamera(FakeCamera):
    def __init__(self, interval=0.005, **kwargs):
        super().__init__(**kwargs)
        self.interval = interval
        self.completed = []

    def capture_request(self, wait=False, flush=None):
        super().capture_request(wait=wait, flush=flush)
        request = CountedRequest(self.capture_count * 100, value=self.capture_count)
        self.completed.append(request)
        return TimedJob(self, request)


class KeepAliveCaptureTests(unittest.TestCase):
    def make_session(self, camera=None, *, clock=None, **overrides):
        camera = camera or StreamingCamera()
        opened = []

        def factory():
            opened.append(camera)
            return camera

        options = dict(memory_budget_mb=8, frame_timeout=0.3)
        options.update(overrides)
        session = KeepAliveFrameCapture(
            CaptureConfig(**options), CameraSettings(5000, 2.0, (1.5, 1.6)),
            camera_factory=factory, available_memory_bytes=lambda: 2**30,
            sensor_clock_ns=clock or (lambda: 0),
        )
        self.addCleanup(self.close_safely, session)
        return session, camera, opened

    @staticmethod
    def close_safely(session):
        with suppress(FrameUnavailable):
            session.close(timeout=0.5)

    def assert_camera_kept_alive(self, session, camera):
        self.assertTrue(session.stats()["camera_started"])
        self.assertEqual(camera.calls.count("start"), 1)
        self.assertEqual(camera.stop_count, 0)
        self.assertEqual(camera.close_count, 0)
        self.assertEqual(camera.cancel_count, 0)

    def test_single_continuous_and_single_share_one_started_camera(self):
        session, camera, opened = self.make_session()
        first = session.capture_one()
        session.start_continuous(max_duration=1)
        session.get_before(STAMP_BASE + 400, timeout=1)
        session.stop_continuous()
        last = session.capture_one()

        self.assertEqual(opened, [camera])
        self.assertEqual(camera.calls.count("configure"), 1)
        self.assertEqual(camera.calls.count("set_controls"), 1)
        self.assert_camera_kept_alive(session, camera)
        self.assertGreater(last.sensor_timestamp_ns, first.sensor_timestamp_ns)
        self.assertEqual(session.stats()["state"], "idle")
        self.assertFalse(camera.controls["AeEnable"])
        self.assertFalse(camera.controls["AwbEnable"])
        self.assertEqual(camera.controls["ExposureTime"], 5000)
        self.assertEqual(camera.config["main"]["size"], (8, 6))
        self.assertEqual(camera.config["sensor"]["output_size"], (8, 6))
        session.close()
        session.close()
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)

    def test_single_copies_exactly_one_and_returned_bytes_survive_reuse(self):
        session, camera, _ = self.make_session()
        first = session.capture_one()
        self.assertEqual(camera.capture_count, 1)
        self.assertEqual(camera.completed[0].copy_count, 1)
        self.assertEqual(camera.completed[0].release_count, 1)
        self.assertEqual(int(camera.completed[0].image[0, 0, 0]), 255)
        session.capture_one()
        self.assertEqual(int(first.image[0, 0, 0]), 1)
        self.assertEqual(first.metadata["ExposureTime"], 5000)
        self.assertFalse(first.image.flags.writeable)

    def test_idle_does_not_request_copy_or_expire_frame_timeout(self):
        session, camera, _ = self.make_session(frame_timeout=0.04)
        session.capture_one()
        before = camera.capture_count
        copied = sum(request.copy_count for request in camera.completed)
        time.sleep(0.15)
        self.assertEqual(camera.capture_count, before)
        self.assertEqual(sum(request.copy_count for request in camera.completed), copied)
        self.assertIsNone(session.stats()["error"])
        self.assert_camera_kept_alive(session, camera)
        self.assertGreater(session.capture_one().sensor_timestamp_ns, STAMP_BASE + 100)

    def test_each_window_takes_new_sensor_clock_boundary_without_internal_flush(self):
        clocks = iter((11, 22, 33))
        session, camera, _ = self.make_session(clock=lambda: next(clocks))
        session.capture_one()
        boundaries = [session.stats()["last_capture"]["fresh_after_timestamp_ns"]]
        session.start_continuous(max_duration=1)
        boundaries.append(session.stats()["current_capture"]["fresh_after_timestamp_ns"])
        session.get_before(STAMP_BASE + 400, timeout=1)
        session.stop_continuous()
        session.capture_one()
        boundaries.append(session.stats()["last_capture"]["fresh_after_timestamp_ns"])
        self.assertEqual(boundaries, [11, 22, 33])
        self.assertTrue(all(call["flush"] is None for call in camera.capture_kwargs))

    def test_freshness_discards_exposure_started_before_activation_without_copy(self):
        camera = FakeCamera()
        stale, fresh = CountedRequest(100), CountedRequest(300)
        camera.requests.put(stale)
        camera.requests.put(fresh)
        session, _, _ = self.make_session(camera, clock=lambda: 5_000_200)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, STAMP_BASE + 300)
        self.assertEqual(stale.copy_count, 0)
        self.assertEqual(stale.release_count, 1)
        self.assertEqual(fresh.copy_count, 1)

    def test_freshness_accepts_exposure_started_exactly_at_activation(self):
        camera = FakeCamera()
        camera.requests.put(CountedRequest(100))
        session, _, _ = self.make_session(camera, clock=lambda: 5_000_100)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, STAMP_BASE + 100)

    def test_steady_old_exposures_do_not_fake_no_frame_timeout_but_fresh_wait_is_bounded(self):
        session, camera, _ = self.make_session(
            StreamingCamera(interval=0.005), clock=lambda: 1_000_000_000,
            frame_timeout=0.03, start_timeout=0.10)
        started = time.monotonic()
        with self.assertRaises(FrameUnavailable) as raised:
            session.capture_one()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 0.6)
        self.assertIn("fresh", str(raised.exception).lower())
        wait_until(lambda: camera.close_count == 1)
        self.assertGreater(camera.capture_count, 6)
        self.assertEqual(sum(request.copy_count for request in camera.completed), 0)

    def test_pending_pause_drains_releases_without_copy_and_resumes_cleanly(self):
        camera = FakeCamera()
        first, drained, resumed = (CountedRequest(stamp) for stamp in (100, 200, 300))
        camera.requests.put(first)
        session, _, _ = self.make_session(camera)
        session.start_continuous(max_duration=1)
        wait_until(lambda: camera.capture_count == 2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            stopped = pool.submit(session.stop_continuous, timeout=0.5)
            wait_until(lambda: session.stats()["state"] != "running")
            camera.requests.put(drained)
            stopped.result(timeout=1)
        self.assertEqual(first.copy_count, 1)
        self.assertEqual(drained.copy_count, 0)
        self.assertEqual(drained.release_count, 1)
        self.assert_camera_kept_alive(session, camera)
        camera.requests.put(resumed)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, STAMP_BASE + 300)
        self.assertEqual(resumed.copy_count, 1)

    def test_new_window_has_no_prior_window_history(self):
        session, camera, _ = self.make_session()
        session.start_continuous(max_duration=1)
        self.assertEqual(session.get_before(STAMP_BASE + 200, timeout=1).sensor_timestamp_ns, STAMP_BASE + 100)
        session.stop_continuous()
        session.start_continuous(max_duration=1)
        with self.assertRaises(FrameUnavailable) as raised:
            session.get_before(STAMP_BASE + 200, timeout=1)
        self.assertEqual(raised.exception.reason, "too_old")
        session.stop_continuous()
        self.assert_camera_kept_alive(session, camera)

    def test_deadline_pauses_without_client_polling_and_can_resume(self):
        session, camera, _ = self.make_session(frame_timeout=0.06)
        session.start_continuous(max_duration=0.035)
        time.sleep(0.15)  # No stats/query/stop is called to drive auto-pause.
        count = camera.capture_count
        time.sleep(0.08)
        self.assertEqual(camera.capture_count, count)
        status = session.stats()
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["stop_reason"], "duration_limit")
        self.assertIsNone(status["error"])
        self.assert_camera_kept_alive(session, camera)
        session.capture_one()
        self.assertEqual(camera.capture_count, count + 1)

    def test_deadline_with_stuck_pending_request_escalates_to_frame_timeout(self):
        session, camera, _ = self.make_session(FakeCamera(), frame_timeout=0.07)
        session.start_continuous(max_duration=0.02)
        wait_until(lambda: camera.close_count == 1)
        self.assertIn("timed out", session.stats()["error"])
        self.assertEqual(camera.stop_count, 1)

    def test_query_from_previous_generation_cannot_read_a_resumed_window(self):
        camera = FakeCamera()
        camera.requests.put(CountedRequest(100))
        session, _, _ = self.make_session(camera)
        session.start_continuous(max_duration=1)
        wait_until(lambda: camera.capture_count == 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            old_query = pool.submit(session.get_before, STAMP_BASE + 1000, timeout=1)
            stopped = pool.submit(session.stop_continuous, timeout=0.5)
            wait_until(lambda: session.stats()["state"] == "stopping")
            camera.requests.put(CountedRequest(200))
            stopped.result(timeout=1)
            camera.requests.put(CountedRequest(300))
            camera.requests.put(CountedRequest(1000))
            session.start_continuous(max_duration=1)
            self.assertEqual(session.get_before(STAMP_BASE + 1000, timeout=1).sensor_timestamp_ns, STAMP_BASE + 300)
            with self.assertRaises(FrameUnavailable) as raised:
                old_query.result(timeout=1)
            self.assertEqual(raised.exception.reason, "stopped")

    def test_query_timeout_never_returns_provisional_frame(self):
        camera = FakeCamera()
        camera.requests.put(CountedRequest(100))
        session, _, _ = self.make_session(camera)
        session.start_continuous(max_duration=1)
        with self.assertRaises(FrameUnavailable) as raised:
            session.get_before(STAMP_BASE + 150, timeout=0.025)
        self.assertEqual(raised.exception.reason, "timeout")
        camera.requests.put(CountedRequest(140))
        camera.requests.put(CountedRequest(150))
        self.assertEqual(session.get_before(STAMP_BASE + 150, timeout=1).sensor_timestamp_ns, STAMP_BASE + 140)

    def test_overlapping_capture_owners_rejected(self):
        camera = FakeCamera()
        session, _, opened = self.make_session(camera)
        with ThreadPoolExecutor(max_workers=1) as pool:
            single = pool.submit(session.capture_one)
            wait_until(lambda: camera.capture_count == 1)
            with self.assertRaises(FrameUnavailable):
                session.capture_one()
            with self.assertRaises(FrameUnavailable):
                session.start_continuous(max_duration=1)
            camera.requests.put(CountedRequest(100))
            single.result(timeout=1)
        self.assertEqual(opened, [camera])

    def test_no_new_frame_timeout_closes_and_blocks_reuse(self):
        session, camera, opened = self.make_session(FakeCamera(), frame_timeout=0.025)
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        wait_until(lambda: camera.close_count == 1)
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        self.assertEqual(opened, [camera])
        self.assertEqual(camera.close_count, 1)
        self.assertIsNotNone(session.stats()["error"])

    def test_start_failure_closes_camera_once_and_does_not_retry(self):
        camera = FakeCamera(start_error=RuntimeError("simulated start failure"))
        session, _, opened = self.make_session(camera)
        with self.assertRaises(Exception) as raised:
            session.capture_one()
        self.assertIn("simulated start failure", str(raised.exception))
        wait_until(lambda: camera.close_count == 1)
        with self.assertRaises(FrameUnavailable):
            session.start_continuous(max_duration=1)
        self.assertEqual(opened, [camera])

    def test_image_copy_failure_releases_owned_request_and_closes(self):
        camera = FakeCamera()
        request = CountedRequest(100, bad_array=True)
        camera.requests.put(request)
        session, _, _ = self.make_session(camera)
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        wait_until(lambda: camera.close_count == 1)
        self.assertEqual(request.release_count, 1)

    def test_native_image_copy_hang_is_bounded_without_second_camera_owner(self):
        entered, release = threading.Event(), threading.Event()

        class HangingRequest(CountedRequest):
            def make_array(self, name):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release copy")
                return super().make_array(name)

        camera = FakeCamera()
        request = HangingRequest(100)
        camera.requests.put(request)
        session, _, opened = self.make_session(camera, frame_timeout=0.025)
        session.CLEANUP_TIMEOUT = 0.04
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.capture_one)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(FrameUnavailable):
                    result.result(timeout=0.5)
                with self.assertRaises(FrameUnavailable):
                    session.start_continuous(max_duration=1)
                self.assertEqual(opened, [camera])
            finally:
                release.set()
        wait_until(lambda: camera.close_count == 1)
        self.assertEqual(request.release_count, 1)

    def test_native_request_submission_hang_is_bounded_and_late_result_released(self):
        entered, release = threading.Event(), threading.Event()

        class HangingCapture(FakeCamera):
            def capture_request(self, wait=False, flush=None):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release request submission")
                return super().capture_request(wait=wait, flush=flush)

        camera = HangingCapture()
        request = CountedRequest(100)
        camera.requests.put(request)
        session, _, opened = self.make_session(camera, frame_timeout=0.025)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.capture_one)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(FrameUnavailable):
                    result.result(timeout=0.5)
                with self.assertRaises(FrameUnavailable):
                    session.capture_one()
                self.assertEqual(opened, [camera])
            finally:
                release.set()
        wait_until(lambda: camera.close_count == 1)
        self.assertEqual(request.release_count, 1)
        self.assertEqual(request.copy_count, 0)

    def test_native_start_hang_honors_start_timeout(self):
        entered, release = threading.Event(), threading.Event()

        class HangingStart(FakeCamera):
            def start(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release start")
                super().start()

        session, camera, opened = self.make_session(HangingStart(), start_timeout=0.025)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(session.start_continuous, max_duration=1)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(FrameUnavailable):
                    result.result(timeout=0.5)
                with self.assertRaises(FrameUnavailable):
                    session.capture_one()
                self.assertEqual(opened, [camera])
            finally:
                release.set()
        wait_until(lambda: camera.close_count == 1)

    def test_pause_during_first_camera_start_does_not_activate_cancelled_window(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedStart(StreamingCamera):
            def start(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release start")
                super().start()

        session, camera, _ = self.make_session(DelayedStart())
        with ThreadPoolExecutor(max_workers=2) as pool:
            starting = pool.submit(session.start_continuous, max_duration=1)
            try:
                self.assertTrue(entered.wait(1))
                stopping = pool.submit(session.stop_continuous, timeout=1)
                wait_until(lambda: session.stats()["state"] == "stopping")
            finally:
                release.set()
            stopping.result(timeout=1)
            with self.assertRaises(FrameUnavailable) as raised:
                starting.result(timeout=1)
            self.assertEqual(raised.exception.reason, "stopped")
        self.assertEqual(camera.capture_count, 0)
        self.assertEqual(session.stats()["state"], "idle")
        self.assert_camera_kept_alive(session, camera)
        session.capture_one()
        self.assertEqual(camera.capture_count, 1)

    def test_close_during_first_camera_start_never_activates_window(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedStart(StreamingCamera):
            def start(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release start")
                super().start()

        session, camera, _ = self.make_session(DelayedStart())
        with ThreadPoolExecutor(max_workers=2) as pool:
            starting = pool.submit(session.start_continuous, max_duration=1)
            try:
                self.assertTrue(entered.wait(1))
                closing = pool.submit(session.close, timeout=1)
                wait_until(lambda: session.stats()["state"] == "stopping")
            finally:
                release.set()
            closing.result(timeout=1)
            with self.assertRaises(FrameUnavailable):
                starting.result(timeout=1)
        self.assertEqual(camera.capture_count, 0)
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)
        self.assertEqual(session.stats()["state"], "closed")

    def test_close_unblocks_start_caller_while_native_start_remains_hung(self):
        entered, release = threading.Event(), threading.Event()

        class HangingStart(StreamingCamera):
            def start(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release start")
                super().start()

        session, camera, opened = self.make_session(HangingStart(), start_timeout=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            starting = pool.submit(session.start_continuous, max_duration=1)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(FrameUnavailable) as closed:
                    session.close(timeout=0.025)
                self.assertEqual(closed.exception.reason, "timeout")
                with self.assertRaises(FrameUnavailable) as interrupted:
                    starting.result(timeout=0.3)
                self.assertIn(interrupted.exception.reason, {"closed", "stopped"})
                self.assertFalse(release.is_set())
                self.assertEqual(camera.close_count, 0)
                with self.assertRaises(FrameUnavailable):
                    session.capture_one()
                self.assertEqual(opened, [camera])
            finally:
                release.set()
        wait_until(lambda: camera.close_count == 1)
        self.assertEqual(camera.capture_count, 0)

    def test_close_unblocks_single_caller_while_native_copy_remains_hung(self):
        entered, release = threading.Event(), threading.Event()

        class HangingRequest(CountedRequest):
            def make_array(self, name):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release copy")
                return super().make_array(name)

        camera = FakeCamera()
        request = HangingRequest(100)
        camera.requests.put(request)
        session, _, opened = self.make_session(camera, frame_timeout=1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            single = pool.submit(session.capture_one)
            try:
                self.assertTrue(entered.wait(1))
                with self.assertRaises(FrameUnavailable) as closed:
                    session.close(timeout=0.025)
                self.assertEqual(closed.exception.reason, "timeout")
                with self.assertRaises(FrameUnavailable) as interrupted:
                    single.result(timeout=0.3)
                self.assertIn(interrupted.exception.reason, {"closed", "stopped"})
                self.assertFalse(release.is_set())
                self.assertEqual(request.release_count, 0)
                with self.assertRaises(FrameUnavailable):
                    session.start_continuous(max_duration=1)
                self.assertEqual(opened, [camera])
            finally:
                release.set()
        wait_until(lambda: camera.close_count == 1)
        self.assertEqual(request.release_count, 1)

    def test_close_failure_is_visible_and_not_retried_natively(self):
        class BrokenClose(StreamingCamera):
            def close(self):
                super().close()
                raise RuntimeError("simulated final close failure")

        session, camera, _ = self.make_session(BrokenClose())
        session.capture_one()
        with self.assertRaises(FrameUnavailable) as raised:
            session.close()
        self.assertIn("simulated final close failure", str(raised.exception))
        with self.assertRaises(FrameUnavailable):
            session.close()
        self.assertEqual(camera.close_count, 1)

    def test_close_hang_is_bounded_and_retains_single_owner(self):
        entered, release = threading.Event(), threading.Event()

        class HangingClose(StreamingCamera):
            def close(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release close")
                super().close()

        session, camera, opened = self.make_session(HangingClose())
        session.capture_one()
        try:
            with self.assertRaises(FrameUnavailable):
                session.close(timeout=0.025)
            self.assertTrue(entered.is_set())
            with self.assertRaises(FrameUnavailable):
                session.capture_one()
            self.assertEqual(opened, [camera])
        finally:
            release.set()
        wait_until(lambda: camera.close_count == 1)
        session.close()
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)

    def test_close_before_first_capture_never_opens_camera(self):
        session, camera, opened = self.make_session()
        session.close()
        session.close()
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        self.assertEqual(opened, [])
        self.assertEqual(camera.close_count, 0)

    def test_invalid_duration_rejected_before_opening(self):
        session, _, opened = self.make_session()
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                session.start_continuous(max_duration=value)
        self.assertEqual(opened, [])

    def test_triggered_inner_glue_rounds_reuse_device_and_keep_delay_idle(self):
        session, camera, opened = self.make_session()
        timers = []

        def timer_factory(delay, callback, args=()):
            timer = ManualTimer(delay, callback, args)
            timers.append(timer)
            return timer

        flow = TriggeredFrameCapture(session, TriggerConfig(), timer_factory=timer_factory)
        self.addCleanup(flow.close)
        inner = flow.on_inner()
        self.assertEqual(timers[0].delay, 3.0)
        self.assertEqual(camera.capture_count, 1)
        self.assert_camera_kept_alive(session, camera)
        timers[0].fire()
        glue = flow.on_glue(STAMP_BASE + 400)
        self.assertEqual(glue.sensor_timestamp_ns, STAMP_BASE + 300)
        self.assertEqual(flow.stats()["state"], "idle")
        second = flow.on_inner()
        self.assertGreater(second.sensor_timestamp_ns, inner.sensor_timestamp_ns)
        self.assertEqual(opened, [camera])
        self.assert_camera_kept_alive(session, camera)
        flow.close()
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)


if __name__ == "__main__":
    unittest.main()
