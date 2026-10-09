"""Native-resolution defaults must preserve the sensor size without scaling."""

from copy import deepcopy
import unittest

from frame_capture import CameraSettings, CaptureConfig, FrameUnavailable, Picamera2FrameCapture
from frame_capture.tests.fakes import FakeCamera, FakeRequest


class NativeResolutionTests(unittest.TestCase):
    def make_capture(self, camera, *, available_bytes=2**30):
        capture = Picamera2FrameCapture(
            CaptureConfig(), CameraSettings(5000, 2.0, (1.5, 1.6)),
            camera_factory=lambda: camera,
            available_memory_bytes=lambda: available_bytes,
        )
        self.addCleanup(capture.stop)
        return capture

    def test_default_uses_reported_native_size_for_sensor_and_images(self):
        camera = FakeCamera(sensor_resolution=(16, 12))
        capture = self.make_capture(camera)
        camera.requests.put(FakeRequest(100, shape=(12, 16, 3)))
        camera.requests.put(FakeRequest(200, shape=(12, 16, 3)))
        capture.start()
        selected = capture.get_before(200, timeout=1)
        self.assertIsNone(capture.config.width)
        self.assertIsNone(capture.config.height)
        self.assertIsNone(capture.config.memory_budget_mb)
        self.assertEqual(camera.config["main"]["size"], (16, 12))
        self.assertEqual(camera.config["sensor"]["output_size"], (16, 12))
        self.assertEqual(selected.image.shape, (12, 16, 3))
        self.assertEqual(capture.stats()["native_sensor_resolution"], (16, 12))
        self.assertEqual(capture.stats()["requested_configuration"]["resolution_mode"], "native")

    def test_negotiated_downscaling_of_main_or_sensor_is_rejected(self):
        for changed_stream in ("main", "sensor"):
            with self.subTest(changed_stream=changed_stream):
                class DownscalingCamera(FakeCamera):
                    def camera_configuration(self):
                        actual = deepcopy(super().camera_configuration())
                        key = "size" if changed_stream == "main" else "output_size"
                        actual[changed_stream][key] = (8, 6)
                        return actual

                camera = DownscalingCamera(sensor_resolution=(16, 12))
                capture = self.make_capture(camera)
                with self.assertRaises(FrameUnavailable) as error:
                    capture.start()
                self.assertEqual(error.exception.reason, "capture_failed")
                self.assertIn("ValueError", str(error.exception))
                self.assertEqual(camera.config["main"]["size"], (16, 12))
                self.assertEqual(camera.config["sensor"]["output_size"], (16, 12))
                self.assertNotIn("start", camera.calls)

    def test_30_native_rgb_frames_refuse_insufficient_ram_without_allocating_images(self):
        # Only dimensions are simulated; no full-resolution image is allocated.
        camera = FakeCamera(sensor_resolution=(4056, 3040))
        capture = self.make_capture(camera, available_bytes=1024**3)
        with self.assertRaises(FrameUnavailable) as error:
            capture.start()
        self.assertEqual(error.exception.reason, "capture_failed")
        self.assertIn("MemoryError", str(error.exception))
        self.assertEqual(camera.config["main"]["size"], (4056, 3040))
        self.assertEqual(camera.config["sensor"]["output_size"], (4056, 3040))
        self.assertEqual(capture.config.capacity, 30)
        self.assertEqual(capture.stats()["count"], 0)
        self.assertEqual(camera.capture_count, 0)
        self.assertNotIn("start", camera.calls)

    def test_explicit_dimensions_must_be_provided_as_a_pair(self):
        for dimensions in ({"width": 16}, {"height": 12}):
            with self.subTest(dimensions=dimensions):
                with self.assertRaises(ValueError):
                    CaptureConfig(**dimensions)


if __name__ == "__main__":
    unittest.main()
