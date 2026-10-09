"""Standalone Picamera2 continuous capture, with no GUI or inference dependency."""
from __future__ import annotations

from concurrent.futures import CancelledError, TimeoutError as JobTimeout
from dataclasses import dataclass
from copy import deepcopy
import json
import math
from numbers import Integral
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .buffer import FrameRecord, FrameRingBuffer, FrameUnavailable, _nanoseconds


def now_sensor_clock_ns() -> int:
    """Linux boot-clock nanoseconds for local signals on the camera host.

    Do not pass Unix epoch time or another host's boot-clock time. On platforms
    without CLOCK_BOOTTIME this raises rather than silently changing time bases.
    SensorTimestamp is used raw, with the camera stack's readout convention.
    """
    if not hasattr(time, "CLOCK_BOOTTIME"):
        raise RuntimeError("The camera signal clock requires Linux CLOCK_BOOTTIME")
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive and finite")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return result


@dataclass(frozen=True)
class CameraSettings:
    exposure_time_us: int
    analogue_gain: float
    colour_gains: tuple[float, float]

    def __post_init__(self) -> None:
        if isinstance(self.exposure_time_us, bool) or not isinstance(self.exposure_time_us, Integral):
            raise ValueError("exposure_time_us must be a positive integer")
        if self.exposure_time_us <= 0:
            raise ValueError("exposure_time_us must be positive")
        object.__setattr__(self, "exposure_time_us", int(self.exposure_time_us))
        object.__setattr__(self, "analogue_gain", _positive_number(self.analogue_gain, "analogue_gain"))
        if len(self.colour_gains) != 2:
            raise ValueError("colour_gains must contain red and blue gains")
        object.__setattr__(self, "colour_gains", tuple(
            _positive_number(value, "colour_gains") for value in self.colour_gains))

    @classmethod
    def from_json(cls, path: str | Path) -> "CameraSettings":
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
        return cls(data["exposure_time_us"], data["analogue_gain"], tuple(data["colour_gains"]))

    def controls(self) -> dict[str, Any]:
        return {
            "AeEnable": False,
            "AwbEnable": False,
            "ExposureTime": self.exposure_time_us,
            "AnalogueGain": self.analogue_gain,
            "ColourGains": self.colour_gains,
        }


@dataclass(frozen=True)
class CaptureConfig:
    capacity: int = 30
    width: int | None = None
    height: int | None = None
    pixel_format: str = "RGB888"
    fps: float | None = None
    memory_budget_mb: float | None = None
    camera_num: int = 0
    camera_buffer_count: int = 4
    frame_timeout: float = 0.2
    start_timeout: float = 10.0

    def __post_init__(self) -> None:
        for name in ("capacity", "camera_buffer_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must both be None for native resolution, or both be positive integers")
        if self.width is not None:
            for name in ("width", "height"):
                value = getattr(self, name)
                if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                    raise ValueError(f"{name} must be a positive integer")
        if self.camera_buffer_count < 2:
            raise ValueError("camera_buffer_count must be at least 2")
        if isinstance(self.camera_num, bool) or not isinstance(self.camera_num, Integral) or self.camera_num < 0:
            raise ValueError("camera_num must be a non-negative integer")
        if self.pixel_format not in {"RGB888", "BGR888", "YUV420"}:
            raise ValueError("pixel_format must be RGB888, BGR888 or YUV420")
        if self.pixel_format == "YUV420" and self.width is not None and (self.width % 2 or self.height % 2):
            raise ValueError("YUV420 dimensions must be even")
        for name in ("frame_timeout", "start_timeout"):
            object.__setattr__(self, name, _positive_number(getattr(self, name), name))
        if self.memory_budget_mb is not None:
            object.__setattr__(self, "memory_budget_mb", _positive_number(self.memory_budget_mb, "memory_budget_mb"))
        if self.fps is not None:
            object.__setattr__(self, "fps", _positive_number(self.fps, "fps"))


def _available_memory_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    # Desktop fake-camera tests may run without Linux or psutil.
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except ImportError:
        return None


class Picamera2FrameCapture:
    """Single-use capture service. Start, query by timestamp, then stop.

    get_before waits until a frame at or after the target has arrived. Only then
    can the newest strictly earlier frame be selected without missing an
    in-flight image. Timeouts never return an unverified older frame.

    Camera operations belong to a daemon worker. Acquisition waits are bounded;
    if native stop/close hangs, stop raises after its timeout. Process isolation
    is needed for forcibly recovering a stuck native camera driver.
    """

    def __init__(
        self,
        config: CaptureConfig,
        settings: CameraSettings,
        *,
        camera_factory: Callable[[], Any] | None = None,
        available_memory_bytes: Callable[[], int | None] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self.buffer = FrameRingBuffer(config.capacity)
        self._camera_factory = camera_factory
        self._memory_probe = available_memory_bytes or _available_memory_bytes
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "new"
        self._error: str | None = None
        self._cleanup_error: str | None = None
        self._duplicates_dropped = 0
        self._negotiated: dict[str, Any] | None = None
        self._requested: dict[str, Any] | None = None
        self._native_resolution: tuple[int, int] | None = None
        self._effective_memory_budget_bytes: int | None = None
        self._controls: dict[str, Any] | None = None
        self._estimated_bytes = 0
        self._available_bytes: int | None = None

    def start(self) -> "Picamera2FrameCapture":
        deadline = time.monotonic() + self.config.start_timeout
        with self._condition:
            if self._state != "new":
                raise RuntimeError("Capture objects are single-use; construct a new object to restart")
            self._state = "starting"
            self._thread = threading.Thread(target=self._run, name="timestamp-frame-capture", daemon=True)
            self._thread.start()
            while self._state == "starting":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop.set()
                    self._state = "stopping"
                    self._condition.notify_all()
                    raise FrameUnavailable("timeout", "Camera startup timed out; stop() must finish cleanup")
                self._condition.wait(remaining)
            if self._state != "running":
                raise FrameUnavailable("capture_failed", self._error or f"Camera state: {self._state}")
        return self

    def stop(self, timeout: float = 3.0) -> None:
        _positive_number(timeout, "timeout")
        self._stop.set()
        with self._condition:
            if self._state == "new":
                self._state = "stopped"
            elif self._state in {"starting", "running"}:
                self._state = "stopping"
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise FrameUnavailable(
                    "timeout", "Camera cleanup timed out; the worker is still stopping. "
                    "Do not reuse this camera until the process exits.")
        if self._cleanup_error is not None:
            raise FrameUnavailable("capture_failed", self._cleanup_error)
        # _run owns stop/close, so callers never race the native camera object.

    def get_before(
        self, timestamp_ns: int, timeout: float = 1.0, max_age_ns: int | None = None
    ) -> FrameRecord:
        target = _nanoseconds(timestamp_ns)
        if max_age_ns is not None:
            _nanoseconds(max_age_ns, "max_age_ns")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be non-negative and finite")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._state == "failed":
                    raise FrameUnavailable("capture_failed", self._error or "Capture failed")
                if self._state not in {"starting", "running"}:
                    raise FrameUnavailable("stopped", "Capture is not running")
                latest = self.buffer.stats()["latest_timestamp_ns"]
                if latest is not None and latest >= target:
                    return self.buffer.get_before(target, max_age_ns)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FrameUnavailable("timeout", "No frame at or after the target arrived before timeout")
                self._condition.wait(remaining)

    def snapshot(self) -> tuple[FrameRecord, ...]:
        return self.buffer.snapshot()

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                **self.buffer.stats(),
                "state": self._state,
                "error": self._error,
                "cleanup_error": self._cleanup_error,
                "duplicates_dropped": self._duplicates_dropped,
                "native_sensor_resolution": self._native_resolution,
                "requested_configuration": deepcopy(self._requested),
                "negotiated_configuration": deepcopy(self._negotiated),
                "applied_controls": deepcopy(self._controls),
                "estimated_bytes": self._estimated_bytes,
                "effective_memory_budget_bytes": self._effective_memory_budget_bytes,
                "available_bytes_at_start": self._available_bytes,
                "timestamp_clock": "CLOCK_BOOTTIME",
                "timestamp_basis": "raw SensorTimestamp",
            }

    def __enter__(self) -> "Picamera2FrameCapture":
        try:
            return self.start()
        except BaseException as exc:
            try:
                self.stop()
            except Exception as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Camera cleanup also failed: {cleanup_error}")
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.stop()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"Camera cleanup also failed: {cleanup_error}")

    def _make_camera(self) -> Any:
        if self._camera_factory is not None:
            return self._camera_factory()
        from picamera2 import Picamera2
        return Picamera2(self.config.camera_num)

    def _prepare(self, camera: Any) -> None:
        native_requested = self.config.width is None
        native = getattr(camera, "sensor_resolution", None)
        if native is not None:
            native = tuple(native)
            if len(native) != 2 or any(
                isinstance(value, bool) or not isinstance(value, Integral) or value <= 0
                for value in native
            ):
                raise ValueError("Camera returned an invalid native sensor resolution")
            self._native_resolution = tuple(int(value) for value in native)
        if native_requested:
            if self._native_resolution is None:
                raise ValueError("Camera did not report its native sensor resolution")
            requested_size = self._native_resolution
        else:
            requested_size = (self.config.width, self.config.height)
        if self.config.pixel_format == "YUV420" and any(value % 2 for value in requested_size):
            raise ValueError("YUV420 dimensions must be even, including the resolved native resolution")
        self._requested = {
            "resolution_mode": "native" if native_requested else "explicit",
            "main": {"size": requested_size, "format": self.config.pixel_format},
            "sensor": {"output_size": self._native_resolution} if native_requested else None,
        }
        configuration_options = {
            "main": dict(self._requested["main"]),
            "raw": None,
            "buffer_count": self.config.camera_buffer_count,
            "queue": False,
        }
        if native_requested:
            # Native output alone could otherwise be upscaled from a binned
            # sensor mode. Require the sensor readout itself to be native too.
            configuration_options["sensor"] = dict(self._requested["sensor"])
        configuration = camera.create_video_configuration(**configuration_options)
        camera.configure(configuration)
        actual = camera.camera_configuration()
        main = actual["main"]
        sensor = actual.get("sensor")
        self._negotiated = {
            "main": dict(main),
            "sensor": dict(sensor) if sensor else None,
            "raw": dict(actual["raw"]) if actual.get("raw") else None,
            "buffer_count": self.config.camera_buffer_count,
            "queue": False,
        }
        if tuple(main["size"]) != requested_size:
            raise ValueError(f"Camera changed requested dimensions {requested_size} to {main['size']}; "
                             "automatic resolution changes are not permitted")
        if native_requested and (
            not sensor or tuple(sensor.get("output_size", ())) != self._native_resolution
        ):
            raise ValueError(f"Camera did not provide native sensor readout {self._native_resolution}; "
                             f"negotiated sensor configuration: {sensor}")
        if main["format"] != self.config.pixel_format:
            raise ValueError(f"Camera changed requested pixel format to {main['format']}")
        framesize = int(main["framesize"])
        if framesize <= 0:
            raise ValueError("Camera returned an invalid negotiated framesize")
        streams_bytes = sum(int(actual[name]["framesize"]) for name in ("main", "lores", "raw")
                            if actual.get(name))
        # Include transient request copies, camera stream buffers and metadata.
        estimated = framesize * (self.config.capacity + 3)
        estimated += streams_bytes * self.config.camera_buffer_count + 2 * 1024 * 1024
        available = self._memory_probe()
        self._estimated_bytes, self._available_bytes = estimated, available
        headroom = 128 * 1024 * 1024
        explicit_budget = (None if self.config.memory_budget_mb is None
                           else int(self.config.memory_budget_mb * 1024 * 1024))
        if available is None and explicit_budget is None:
            raise MemoryError("Available RAM could not be determined; provide an explicit memory_budget_mb")
        budgets = [value for value in (
            explicit_budget, None if available is None else max(0, available - headroom)
        ) if value is not None]
        self._effective_memory_budget_bytes = min(budgets)
        if explicit_budget is not None and estimated > explicit_budget:
            raise MemoryError(f"Capture needs approximately {estimated / 1024**2:.1f} MiB; "
                              f"budget is {self.config.memory_budget_mb} MiB")
        if available is not None and estimated + headroom > available:
            raise MemoryError(f"Capture needs {estimated / 1024**2:.1f} MiB plus 128 MiB headroom; "
                              f"only {available / 1024**2:.1f} MiB available")

        controls = self.settings.controls()
        ranges = getattr(camera, "camera_controls", {})
        for name, values in (("ExposureTime", (self.settings.exposure_time_us,)),
                             ("AnalogueGain", (self.settings.analogue_gain,)),
                             ("ColourGains", self.settings.colour_gains)):
            if name in ranges:
                low, high, _ = ranges[name]
                lows = low if isinstance(low, (list, tuple)) else (low,) * len(values)
                highs = high if isinstance(high, (list, tuple)) else (high,) * len(values)
                if len(lows) != len(values) or len(highs) != len(values):
                    raise ValueError(f"Invalid camera control range for {name}")
                if any(value < lo or value > hi for value, lo, hi in zip(values, lows, highs)):
                    raise ValueError(f"{name} outside supported camera range {low}..{high}")
        frame_range = ranges.get("FrameDurationLimits")
        if self.config.fps is not None:
            period = round(1_000_000 / self.config.fps)
            if period < self.settings.exposure_time_us:
                raise ValueError("Requested frame period is shorter than the fixed exposure")
            if frame_range is not None and not frame_range[0] <= period <= frame_range[1]:
                raise ValueError("Requested FPS is outside the configured sensor mode's range")
            controls["FrameDurationLimits"] = (period, period)
        elif frame_range is not None:
            # Video configurations default to 30 fps. Remove that cap explicitly,
            # or a saved 66 ms exposure would be silently clipped to ~33 ms.
            low, high, _ = frame_range
            if high < self.settings.exposure_time_us:
                raise ValueError("Fixed exposure exceeds the sensor frame-duration limit")
            controls["FrameDurationLimits"] = (int(low), int(high))
        camera.set_controls(controls)
        self._controls = controls

    @staticmethod
    def _release_pending(camera: Any, pending: Any) -> None:
        if pending is None:
            return
        camera.cancel_all_and_flush()
        try:
            request = pending.get_result(timeout=0.2)
        except CancelledError:
            return
        # A job could complete immediately before cancellation. Release its
        # returned request too, rather than leaving one driver buffer acquired.
        request.release()

    def _run(self) -> None:
        camera = None
        pending = None
        error: Exception | None = None
        try:
            camera = self._make_camera()
            self._prepare(camera)
            if self._stop.is_set():
                return
            camera.start()
            with self._condition:
                if self._stop.is_set():
                    return
                self._state = "running"
                self._condition.notify_all()
            pending = camera.capture_request(wait=False)
            last_progress = time.monotonic()
            sequence = 0
            latest_timestamp = -1
            while not self._stop.is_set():
                try:
                    request = pending.get_result(timeout=0.1)
                except JobTimeout:
                    if time.monotonic() - last_progress > self.config.frame_timeout:
                        raise RuntimeError("Camera frame acquisition timed out")
                    continue
                pending = None
                try:
                    # Keep one acquisition queued while the current image is copied.
                    if not self._stop.is_set():
                        pending = camera.capture_request(wait=False)
                    metadata = request.get_metadata()
                    timestamp = _nanoseconds(metadata["SensorTimestamp"])
                    if timestamp == latest_timestamp:
                        with self._condition:
                            self._duplicates_dropped += 1
                        if time.monotonic() - last_progress > self.config.frame_timeout:
                            raise RuntimeError("Camera repeatedly returned the same frame")
                        continue
                    if timestamp < latest_timestamp:
                        raise RuntimeError("Camera SensorTimestamp went backwards")
                    array = request.make_array("main")
                    if array.nbytes > self._negotiated["main"]["framesize"]:
                        raise MemoryError("Camera image exceeds its negotiated memory size")
                    record = FrameRecord(sequence, timestamp, array, metadata)
                finally:
                    request.release()
                with self._condition:
                    self.buffer.append(record)
                    self._condition.notify_all()
                sequence += 1
                latest_timestamp = timestamp
                last_progress = time.monotonic()
        except Exception as exc:
            error = exc
            with self._condition:
                self._error = f"{type(exc).__name__}: {exc}"
                self._state = "failed"
                self._condition.notify_all()
        finally:
            if camera is not None:
                for cleanup in (
                    lambda: self._release_pending(camera, pending),
                    camera.stop,
                    camera.close,
                ):
                    try:
                        cleanup()
                    except Exception as exc:
                        self._cleanup_error = f"Camera cleanup failed: {type(exc).__name__}: {exc}"
                        if error is None:
                            error = exc
            with self._condition:
                if error is not None:
                    self._error = f"{type(error).__name__}: {error}"
                    self._state = "failed"
                else:
                    self._state = "stopped"
                self._condition.notify_all()
