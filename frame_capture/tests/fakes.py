"""Queue-backed camera doubles that model reusable Picamera2 request buffers."""

import queue
import threading
from concurrent.futures import CancelledError, TimeoutError

import numpy as np


class FakeRequest:
    def __init__(self, timestamp_ns, value=7, shape=(6, 8, 3), *, bad_array=False,
                 exposure_us=5000, analogue_gain=2.0, colour_gains=(1.5, 1.6)):
        self.metadata = {
            "SensorTimestamp": timestamp_ns,
            "ExposureTime": exposure_us,
            "AnalogueGain": analogue_gain,
            "ColourGains": colour_gains,
            "FrameDuration": 20000,
        }
        self.image = np.full(shape, value, dtype=np.uint8)
        self.bad_array = bad_array
        self.release_count = 0

    def get_metadata(self):
        return self.metadata

    def make_array(self, name):
        if name != "main":
            raise AssertionError("Only the configured main stream should be copied")
        if self.bad_array:
            raise RuntimeError("simulated image extraction failure")
        return self.image

    def release(self):
        self.release_count += 1
        # Emulate reuse by the camera immediately after the request is returned.
        self.image.fill(255)
        self.metadata["ExposureTime"] = -1


class FakeJob:
    def __init__(self, camera):
        self.camera = camera
        self.cancelled = False

    def get_result(self, timeout=None):
        if self.cancelled or self.camera.cancelled.is_set():
            raise CancelledError("fake camera stopped")
        try:
            result = self.camera.requests.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError() from None
        if isinstance(result, BaseException):
            raise result
        return result

    def cancel(self):
        self.cancelled = True


class FakeCamera:
    def __init__(self, *, framesize=None, start_error=None, sensor_resolution=(8, 6)):
        self.requests = queue.Queue()
        self.cancelled = threading.Event()
        self.camera_controls = {
            "ExposureTime": (100, 200000, 5000),
            "AnalogueGain": (1.0, 32.0, 1.0),
            "ColourGains": ((0.0, 0.0), (32.0, 32.0), (1.0, 1.0)),
            "FrameDurationLimits": (1000, 1000000, 33333),
        }
        self.sensor_resolution = sensor_resolution
        self.framesize = framesize
        self.start_error = start_error
        self.config = None
        self.controls = None
        self.calls = []
        self.capture_count = 0
        self.stop_count = 0
        self.close_count = 0
        self.cancel_count = 0

    def create_video_configuration(self, **kwargs):
        self.calls.append("create_video_configuration")
        main = dict(kwargs.get("main", {}))
        main.setdefault("size", self.sensor_resolution)
        main.setdefault("format", "RGB888")
        width, height = main["size"]
        main["framesize"] = self.framesize or width * height * 3
        sensor = dict(kwargs.get("sensor") or {"output_size": main["size"]})
        return {**kwargs, "main": main, "sensor": sensor}

    def configure(self, config):
        self.calls.append("configure")
        self.config = config

    def camera_configuration(self):
        return self.config

    def set_controls(self, controls):
        self.calls.append("set_controls")
        self.controls = dict(controls)

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def capture_request(self, wait=False):
        if wait is not False:
            raise AssertionError("capture must use an interruptible asynchronous job")
        self.capture_count += 1
        return FakeJob(self)

    def cancel_all_and_flush(self):
        self.calls.append("cancel_all_and_flush")
        self.cancel_count += 1
        self.cancelled.set()

    def stop(self):
        self.calls.append("stop")
        self.stop_count += 1

    def close(self):
        self.calls.append("close")
        self.close_count += 1
