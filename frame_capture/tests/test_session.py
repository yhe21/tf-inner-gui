"""On-demand camera sessions use real worker logic with fake camera requests."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from frame_capture import CameraSettings, CaptureConfig, FrameUnavailable
from frame_capture.session import OnDemandFrameCapture
from frame_capture.tests.fakes import FakeCamera, FakeRequest
from frame_capture.tests.test_camera import wait_until


class OnDemandSessionTests(unittest.TestCase):
    def make_session(self, *cameras, **config_overrides):
        queued = iter(cameras)
        opened = []

        def factory():
            camera = next(queued)
            opened.append(camera)
            return camera

        options = dict(memory_budget_mb=8, frame_timeout=0.5)
        options.update(config_overrides)
        session = OnDemandFrameCapture(
            CaptureConfig(**options), CameraSettings(5000, 2.0, (1.5, 1.6)),
            camera_factory=factory, available_memory_bytes=lambda: 2**30,
        )
        self.addCleanup(self.close_safely, session)
        return session, opened

    @staticmethod
    def close_safely(session):
        with suppress(FrameUnavailable):
            session.close()

    def test_defaults_keep_native_resolution_30_frames_and_200ms_wait(self):
        config = CaptureConfig()
        self.assertEqual(config.capacity, 30)
        self.assertIsNone(config.width)
        self.assertIsNone(config.height)
        self.assertEqual(config.frame_timeout, 0.2)

    def test_single_copies_only_one_frame_and_closes_before_return(self):
        camera = FakeCamera()
        first, unused = FakeRequest(100, value=13), FakeRequest(200, value=22)
        camera.requests.put(first)
        camera.requests.put(unused)
        session, opened = self.make_session(camera)

        frame = session.capture_one()

        self.assertEqual(opened, [camera])
        self.assertEqual(camera.capture_count, 1)
        self.assertEqual(first.release_count, 1)
        self.assertEqual(unused.release_count, 0)
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)
        self.assertEqual(frame.sensor_timestamp_ns, 100)
        self.assertEqual(frame.image.shape, (6, 8, 3))
        self.assertEqual(int(frame.image[0, 0, 0]), 13)
        self.assertEqual(frame.metadata["ExposureTime"], 5000)
        self.assertFalse(frame.image.flags.writeable)
        self.assertFalse(camera.controls["AeEnable"])
        self.assertFalse(camera.controls["AwbEnable"])
        self.assertEqual(camera.controls["ExposureTime"], 5000)

    def test_returned_single_survives_repeated_single_and_continuous_sessions(self):
        cameras = [FakeCamera() for _ in range(3)]
        cameras[0].requests.put(FakeRequest(100, value=13))
        cameras[1].requests.put(FakeRequest(200, value=21))
        cameras[2].requests.put(FakeRequest(300, value=31))
        cameras[2].requests.put(FakeRequest(400, value=41))
        session, opened = self.make_session(*cameras)

        first = session.capture_one()
        second = session.capture_one()
        session.start_continuous(max_duration=1)
        selected = session.get_before(400, timeout=1)
        session.stop_continuous()

        self.assertEqual(len(opened), 3)
        self.assertTrue(all(camera.close_count == 1 for camera in cameras))
        self.assertEqual(first.sensor_timestamp_ns, 100)
        self.assertEqual(int(first.image[0, 0, 0]), 13)
        self.assertEqual(second.sensor_timestamp_ns, 200)
        self.assertEqual(int(second.image[0, 0, 0]), 21)
        self.assertEqual(selected.sensor_timestamp_ns, 300)

    def test_new_continuous_session_never_returns_previous_session_history(self):
        cameras = [FakeCamera(), FakeCamera()]
        for stamp in (100, 200):
            cameras[0].requests.put(FakeRequest(stamp))
        for stamp in (300, 400):
            cameras[1].requests.put(FakeRequest(stamp))
        session, _ = self.make_session(*cameras)
        session.start_continuous(max_duration=1)
        self.assertEqual(session.get_before(200, timeout=1).sensor_timestamp_ns, 100)
        session.stop_continuous()
        session.start_continuous(max_duration=1)
        with self.assertRaises(FrameUnavailable) as error:
            session.get_before(200, timeout=1)
        self.assertEqual(error.exception.reason, "too_old")
        self.assertEqual(session.get_before(400, timeout=1).sensor_timestamp_ns, 300)

    def test_deadline_closes_device_without_any_client_poll_or_stop(self):
        camera = FakeCamera()
        camera.requests.put(FakeRequest(100))
        closed = threading.Event()
        original_close = camera.close

        def close():
            original_close()
            closed.set()

        camera.close = close
        session, _ = self.make_session(camera, frame_timeout=1)
        started = time.monotonic()
        session.start_continuous(max_duration=0.05)

        self.assertTrue(closed.wait(0.6), "automatic stop must not require stats/get_before")
        self.assertLess(time.monotonic() - started, 0.7)
        self.assertEqual(camera.close_count, 1)
        with self.assertRaises(FrameUnavailable):
            session.get_before(101, timeout=0.05)

    def test_deadline_stop_is_reusable_after_clean_shutdown(self):
        cameras = [FakeCamera(), FakeCamera()]
        cameras[0].requests.put(FakeRequest(100))
        cameras[1].requests.put(FakeRequest(200))
        session, _ = self.make_session(*cameras)
        session.start_continuous(max_duration=0.03)
        wait_until(lambda: cameras[0].close_count == 1)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, 200)

    def test_active_continuous_session_rejects_second_owner(self):
        camera = FakeCamera()
        session, opened = self.make_session(camera)
        session.start_continuous(max_duration=1)
        with self.assertRaises(RuntimeError):
            session.capture_one()
        with self.assertRaises(RuntimeError):
            session.start_continuous(max_duration=1)
        self.assertEqual(opened, [camera])

    def test_pending_single_rejects_second_owner(self):
        camera = FakeCamera()
        session, opened = self.make_session(camera)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(session.capture_one)
            wait_until(lambda: camera.capture_count == 1)
            with self.assertRaises(RuntimeError):
                session.capture_one()
            with self.assertRaises(RuntimeError):
                session.start_continuous(max_duration=1)
            camera.requests.put(FakeRequest(100))
            self.assertEqual(future.result(timeout=1).sensor_timestamp_ns, 100)
        self.assertEqual(opened, [camera])

    def test_query_waits_for_watermark_and_never_returns_provisional_frame(self):
        camera = FakeCamera()
        camera.requests.put(FakeRequest(100))
        session, _ = self.make_session(camera)
        session.start_continuous(max_duration=1)
        with self.assertRaises(FrameUnavailable) as error:
            session.get_before(150, timeout=0.02)
        self.assertEqual(error.exception.reason, "timeout")
        camera.requests.put(FakeRequest(140))
        camera.requests.put(FakeRequest(150))
        self.assertEqual(session.get_before(150, timeout=1).sensor_timestamp_ns, 140)
        with self.assertRaises(FrameUnavailable) as error:
            session.get_before(150, timeout=0.1, max_age_ns=9)
        self.assertEqual(error.exception.reason, "too_stale")

    def test_cleanup_failure_is_visible_and_blocks_new_camera_owner(self):
        class BrokenCloseCamera(FakeCamera):
            def close(self):
                super().close()
                raise RuntimeError("simulated close failure")

        bad, unused = BrokenCloseCamera(), FakeCamera()
        bad.requests.put(FakeRequest(100))
        unused.requests.put(FakeRequest(200))
        session, opened = self.make_session(bad, unused)
        with self.assertRaises(FrameUnavailable) as error:
            session.capture_one()
        self.assertIn("simulated close failure", str(error.exception))
        with self.assertRaises(RuntimeError):
            session.capture_one()
        with self.assertRaises(RuntimeError):
            session.start_continuous(max_duration=1)
        self.assertEqual(opened, [bad])

    def test_missing_single_frame_times_out_and_cleans_up(self):
        camera = FakeCamera()
        session, _ = self.make_session(camera, frame_timeout=0.025)
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)

    def test_clean_acquisition_failure_allows_explicit_new_session(self):
        failed, next_camera = FakeCamera(), FakeCamera()
        next_camera.requests.put(FakeRequest(200))
        session, _ = self.make_session(failed, next_camera, frame_timeout=0.025)
        with self.assertRaises(FrameUnavailable):
            session.capture_one()
        self.assertEqual(failed.close_count, 1)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, 200)

    def test_cleanup_timeout_keeps_ownership_until_worker_has_really_closed(self):
        close_entered, allow_close = threading.Event(), threading.Event()

        class SlowCloseCamera(FakeCamera):
            def close(self):
                close_entered.set()
                if not allow_close.wait(2):
                    raise AssertionError("test did not release camera close")
                super().close()

        slow, next_camera = SlowCloseCamera(), FakeCamera()
        next_camera.requests.put(FakeRequest(200))
        session, opened = self.make_session(slow, next_camera)
        session.start_continuous(max_duration=0.025)
        try:
            self.assertTrue(close_entered.wait(1))
            with self.assertRaises(FrameUnavailable) as error:
                session.stop_continuous(timeout=0.01)
            self.assertEqual(error.exception.reason, "timeout")
            self.assertEqual(session.stats()["state"], "cleanup_failed")
            with self.assertRaises(FrameUnavailable) as error:
                session.capture_one()
            self.assertEqual(error.exception.reason, "busy")
            self.assertEqual(opened, [slow])
        finally:
            allow_close.set()
        wait_until(lambda: session.stats()["state"] == "idle")
        self.assertEqual(slow.close_count, 1)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, 200)

    def test_watchdog_handles_native_image_copy_that_ignores_wait_timeout(self):
        copying, allow_copy = threading.Event(), threading.Event()

        class SlowRequest(FakeRequest):
            def make_array(self, name):
                copying.set()
                if not allow_copy.wait(2):
                    raise AssertionError("test did not release image copy")
                return super().make_array(name)

        stalled, next_camera = FakeCamera(), FakeCamera()
        request = SlowRequest(100)
        stalled.requests.put(request)
        next_camera.requests.put(FakeRequest(200))
        session, opened = self.make_session(stalled, next_camera, frame_timeout=0.03)
        session.CLEANUP_TIMEOUT = 0.04
        session.start_continuous(max_duration=1)
        try:
            self.assertTrue(copying.wait(1))
            wait_until(lambda: session.stats()["state"] == "cleanup_failed")
            self.assertIn("timed out", session.stats()["error"])
            with self.assertRaises(FrameUnavailable) as error:
                session.get_before(150, timeout=0.01)
            self.assertEqual(error.exception.reason, "capture_failed")
            with self.assertRaises(FrameUnavailable) as error:
                session.start_continuous(max_duration=1)
            self.assertEqual(error.exception.reason, "busy")
            self.assertEqual(opened, [stalled])
        finally:
            allow_copy.set()
        wait_until(lambda: session.stats()["state"] == "idle")
        self.assertEqual(request.release_count, 1)
        self.assertEqual(stalled.close_count, 1)
        self.assertEqual(session.capture_one().sensor_timestamp_ns, 200)

    def test_max_duration_starts_after_camera_startup_finishes(self):
        start_entered, allow_start = threading.Event(), threading.Event()

        class SlowStartCamera(FakeCamera):
            def start(self):
                start_entered.set()
                if not allow_start.wait(1):
                    raise AssertionError("test did not release camera startup")
                super().start()

        camera = SlowStartCamera()
        session, _ = self.make_session(camera)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(session.start_continuous, max_duration=0.06)
            self.assertTrue(start_entered.wait(1))
            time.sleep(0.1)  # Deliberately longer than the capture window itself.
            self.assertEqual(camera.close_count, 0)
            allow_start.set()
            future.result(timeout=1)
            self.assertEqual(camera.close_count, 0)
        wait_until(lambda: session.stats()["state"] == "idle")
        last = session.stats()["last_capture"]
        elapsed = last["stop_requested_monotonic"] - last["started_monotonic"]
        self.assertEqual(last["stop_reason"], "duration_limit")
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 0.4)

    def test_close_is_idempotent_and_terminal(self):
        camera = FakeCamera()
        session, opened = self.make_session(camera)
        session.close()
        session.close()
        with self.assertRaises(RuntimeError):
            session.capture_one()
        with self.assertRaises(RuntimeError):
            session.start_continuous(max_duration=1)
        self.assertEqual(opened, [])

    def test_invalid_duration_rejected_before_opening_camera(self):
        session, opened = self.make_session(FakeCamera())
        for value in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                session.start_continuous(max_duration=value)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
