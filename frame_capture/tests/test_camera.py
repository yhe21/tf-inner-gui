"""Camera adapter contract checks using reusable, asynchronous fake requests."""

import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from frame_capture import (
    CameraSettings,
    CaptureConfig,
    FrameUnavailable,
    Picamera2FrameCapture,
)
from frame_capture.tests.fakes import FakeCamera, FakeRequest


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for the background capture worker")


class CameraCaptureTests(unittest.TestCase):
    def make_capture(self, camera=None, settings=None, **config_overrides):
        camera = camera or FakeCamera()
        values = dict(width=8, height=6, capacity=200, memory_budget_mb=8,
                      frame_timeout=2.0)
        values.update(config_overrides)
        config = CaptureConfig(**values)
        settings = settings or CameraSettings(exposure_time_us=5000, analogue_gain=2.0,
                                  colour_gains=(1.5, 1.6))
        capture = Picamera2FrameCapture(
            config, settings, camera_factory=lambda: camera,
            available_memory_bytes=lambda: 2**30,
        )
        self.addCleanup(capture.stop)
        return capture, camera

    def test_camera_receives_fixed_manual_controls_before_start(self):
        capture, camera = self.make_capture(fps=50)
        capture.start()
        self.assertFalse(camera.controls["AeEnable"])
        self.assertFalse(camera.controls["AwbEnable"])
        self.assertEqual(camera.controls["ExposureTime"], 5000)
        self.assertEqual(camera.controls["AnalogueGain"], 2.0)
        self.assertEqual(tuple(camera.controls["ColourGains"]), (1.5, 1.6))
        self.assertEqual(tuple(camera.controls["FrameDurationLimits"]), (20000, 20000))
        self.assertLess(camera.calls.index("set_controls"), camera.calls.index("start"))

    def test_saved_long_exposure_is_not_clipped_by_default_video_frame_rate(self):
        settings = CameraSettings(exposure_time_us=66657, analogue_gain=4.87619,
                                  colour_gains=(3.04152, 1.50356))
        capture, camera = self.make_capture(settings=settings)
        capture.start()
        self.assertEqual(camera.controls["ExposureTime"], 66657)
        self.assertEqual(tuple(camera.controls["FrameDurationLimits"]), (1000, 1000000))
        self.assertGreaterEqual(camera.controls["FrameDurationLimits"][1], 66657)

    def test_fixed_fps_incompatible_with_exposure_is_rejected(self):
        settings = CameraSettings(exposure_time_us=66657, analogue_gain=2.0,
                                  colour_gains=(1.5, 1.6))
        capture, camera = self.make_capture(settings=settings, fps=30)
        with self.assertRaises(FrameUnavailable) as error:
            capture.start()
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("shorter than the fixed exposure", str(error.exception))
        self.assertNotIn("start", camera.calls)

    def test_image_and_metadata_are_copied_before_camera_request_release(self):
        capture, camera = self.make_capture()
        first = FakeRequest(100, value=13)
        second = FakeRequest(200, value=17)
        camera.requests.put(first)
        camera.requests.put(second)
        capture.start()
        frame = capture.get_before(200, timeout=1)
        wait_until(lambda: first.release_count == 1)
        self.assertEqual(frame.sensor_timestamp_ns, 100)
        self.assertEqual(int(frame.image[0, 0, 0]), 13)
        self.assertEqual(frame.metadata["ExposureTime"], 5000)
        self.assertEqual(int(first.image[0, 0, 0]), 255)
        self.assertEqual(first.metadata["ExposureTime"], -1)
        self.assertEqual(first.release_count, 1)

    def test_lookup_waits_for_inflight_frames_before_selecting_nearest_predecessor(self):
        capture, camera = self.make_capture()
        camera.requests.put(FakeRequest(100))
        capture.start()
        wait_until(lambda: capture.stats()["count"] == 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(capture.get_before, 150, timeout=1)
            time.sleep(0.03)
            self.assertFalse(future.done(), "latest received frame is not yet a watermark")
            camera.requests.put(FakeRequest(140))
            wait_until(lambda: capture.stats()["count"] == 2)
            self.assertFalse(future.done(), "another earlier frame may still be in flight")
            camera.requests.put(FakeRequest(160))
            selected = future.result(timeout=2)
        self.assertEqual(selected.sensor_timestamp_ns, 140)

    def test_timestamp_equal_to_signal_is_never_selected(self):
        capture, camera = self.make_capture()
        for timestamp in (100, 150, 200):
            camera.requests.put(FakeRequest(timestamp))
        capture.start()
        selected = capture.get_before(150, timeout=1)
        self.assertEqual(selected.sensor_timestamp_ns, 100)

    def test_lookup_does_not_return_a_provisional_frame_on_timeout(self):
        capture, camera = self.make_capture()
        camera.requests.put(FakeRequest(100))
        capture.start()
        wait_until(lambda: capture.stats()["count"] == 1)
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(150, timeout=0.05)
        self.assertEqual(error.exception.reason, "timeout")

    def test_duplicate_requests_are_released_and_not_stored(self):
        capture, camera = self.make_capture()
        requests = [FakeRequest(timestamp) for timestamp in (100, 100, 200, 300)]
        for request in requests:
            camera.requests.put(request)
        capture.start()
        selected = capture.get_before(300, timeout=1)
        wait_until(lambda: all(request.release_count == 1 for request in requests))
        self.assertEqual(selected.sensor_timestamp_ns, 200)
        self.assertEqual(capture.stats()["count"], 3)
        self.assertEqual(capture.stats()["duplicates_dropped"], 1)
        self.assertTrue(all(request.release_count == 1 for request in requests))

    def test_regressing_timestamp_fails_and_releases_the_bad_request(self):
        capture, camera = self.make_capture()
        camera.requests.put(FakeRequest(100))
        capture.start()
        wait_until(lambda: capture.stats()["count"] == 1)
        bad_request = FakeRequest(90)
        camera.requests.put(bad_request)
        wait_until(lambda: capture.stats()["state"] == "failed")
        self.assertEqual(bad_request.release_count, 1)
        self.assertEqual(capture.stats()["count"], 1)
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(200, timeout=0.1)
        self.assertEqual(error.exception.reason, "capture_failed")

    def test_copy_failure_releases_request_and_exposes_capture_failure(self):
        capture, camera = self.make_capture()
        bad_request = FakeRequest(100, bad_array=True)
        capture.start()
        camera.requests.put(bad_request)
        wait_until(lambda: capture.stats()["state"] == "failed")
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(200, timeout=0.1)
        self.assertEqual(error.exception.reason, "capture_failed")
        capture.stop()
        self.assertEqual(bad_request.release_count, 1)
        self.assertEqual(camera.close_count, 1)

    def test_missing_sensor_timestamp_fails_without_inventing_host_timestamp(self):
        capture, camera = self.make_capture()
        bad_request = FakeRequest(100)
        del bad_request.metadata["SensorTimestamp"]
        capture.start()
        camera.requests.put(bad_request)
        wait_until(lambda: capture.stats()["state"] == "failed")
        self.assertEqual(capture.stats()["count"], 0)
        self.assertEqual(bad_request.release_count, 1)
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(200, timeout=0.1)
        self.assertEqual(error.exception.reason, "capture_failed")

    def test_stop_interrupts_waiting_capture_and_closes_camera_once(self):
        capture, camera = self.make_capture()
        capture.start()
        wait_until(lambda: camera.capture_count >= 1)
        started = time.monotonic()
        capture.stop(timeout=1)
        self.assertLess(time.monotonic() - started, 1)
        capture.stop(timeout=1)
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(200, timeout=0.1)
        self.assertEqual(error.exception.reason, "stopped")

    def test_camera_start_failure_closes_the_device(self):
        capture, camera = self.make_capture(camera=FakeCamera(start_error=RuntimeError("start failed")))
        with self.assertRaises(FrameUnavailable) as error:
            capture.start()
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("start failed", str(error.exception))
        capture.stop()
        self.assertEqual(camera.close_count, 1)

    def test_missing_frames_eventually_exposes_a_capture_failure(self):
        capture, camera = self.make_capture(frame_timeout=0.1)
        capture.start()
        wait_until(lambda: capture.stats()["state"] == "failed")
        with self.assertRaises(FrameUnavailable) as error:
            capture.get_before(200, timeout=0.1)
        self.assertEqual(error.exception.reason, "capture_failed")

    def test_memory_budget_rejects_requested_ring_before_camera_starts(self):
        camera = FakeCamera()
        with self.assertRaises(FrameUnavailable) as error:
            capture, _ = self.make_capture(camera=camera, width=1280, height=720,
                                           capacity=200, memory_budget_mb=1)
            capture.start()
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("MemoryError", str(error.exception))
        self.assertNotIn("start", camera.calls)

    def test_memory_budget_accounts_for_actual_camera_frame_allocation(self):
        camera = FakeCamera(framesize=1024 * 1024)
        with self.assertRaises(FrameUnavailable) as error:
            capture, _ = self.make_capture(camera=camera, memory_budget_mb=8)
            capture.start()
        self.assertIn("configure", camera.calls)
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("MemoryError", str(error.exception))
        self.assertNotIn("start", camera.calls)


class CameraCleanupTests(unittest.TestCase):
    @staticmethod
    def capture_for(camera):
        return Picamera2FrameCapture(
            CaptureConfig(width=8, height=6, capacity=200, memory_budget_mb=8),
            CameraSettings(5000, 2.0, (1.5, 1.6)),
            camera_factory=lambda: camera,
            available_memory_bytes=lambda: 2**30,
        )

    def test_stop_failure_is_reported_and_close_is_still_attempted(self):
        class StopFailureCamera(FakeCamera):
            def stop(self):
                super().stop()
                raise RuntimeError("simulated stop failure")

        camera = StopFailureCamera()
        capture = self.capture_for(camera)
        capture.start()
        with self.assertRaises(FrameUnavailable) as error:
            capture.stop(timeout=1)
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("simulated stop failure", str(error.exception))
        self.assertEqual(camera.stop_count, 1)
        self.assertEqual(camera.close_count, 1)
        self.assertEqual(capture.stats()["state"], "failed")

    def test_context_start_failure_waits_for_device_cleanup(self):
        camera = FakeCamera(start_error=RuntimeError("simulated context startup failure"))
        capture = self.capture_for(camera)
        with self.assertRaises(FrameUnavailable) as error:
            with capture:
                self.fail("context body cannot run after failed camera startup")
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("simulated context startup failure", str(error.exception))
        self.assertEqual(camera.close_count, 1)

    def test_available_memory_headroom_is_checked_before_capture_starts(self):
        camera = FakeCamera()
        capture = Picamera2FrameCapture(
            CaptureConfig(width=8, height=6, capacity=200, memory_budget_mb=8),
            CameraSettings(5000, 2.0, (1.5, 1.6)),
            camera_factory=lambda: camera,
            available_memory_bytes=lambda: 1024 * 1024,
        )
        try:
            with self.assertRaises(FrameUnavailable) as error:
                capture.start()
            self.assertEqual(error.exception.reason, "capture_failed")
            self.assertIn("headroom", str(error.exception))
            self.assertNotIn("start", camera.calls)
        finally:
            capture.stop()


class CameraSettingsTests(unittest.TestCase):
    def test_existing_three_field_json_is_loaded_without_unit_changes(self):
        contents = {"exposure_time_us": 66657, "analogue_gain": 4.87619,
                    "colour_gains": [3.04152, 1.50356]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera_settings.json"
            path.write_text(json.dumps(contents), encoding="utf-8")
            settings = CameraSettings.from_json(path)
        self.assertEqual(settings.exposure_time_us, 66657)
        self.assertEqual(settings.analogue_gain, 4.87619)
        self.assertEqual(tuple(settings.colour_gains), (3.04152, 1.50356))


if __name__ == "__main__":
    unittest.main()
