"""Keep the camera streaming while application image reads are paused."""
from __future__ import annotations

from concurrent.futures import TimeoutError as JobTimeout
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable

from .buffer import FrameRecord, FrameRingBuffer, FrameUnavailable, _nanoseconds
from .capture import (CameraSettings, CaptureConfig, Picamera2FrameCapture,
                      _positive_number, now_sensor_clock_ns)


@dataclass
class _Operation:
    generation: int
    mode: str
    buffer: FrameRingBuffer
    max_duration: float | None
    state: str = "starting"
    started_at: float | None = None
    progress_at: float | None = None
    stop_requested_at: float | None = None
    stop_reason: str | None = None
    fresh_after_ns: int | None = None
    fresh_ready: bool = False
    discarded_before_activation: int = 0
    duplicates: int = 0
    done: bool = False
    result: FrameRecord | None = None


class KeepAliveFrameCapture:
    """Reusable single shots and bounded windows with one warm camera.

    A worker exclusively owns every camera API call. Between operations it
    waits without requesting or copying images; Picamera2's event loop keeps
    the sensor streaming. A normal pause drains the one pending request and
    releases it without copying. Only close or a fatal failure stops the camera.

    The frame timeout bounds gaps between new sensor completions. Freshness
    filtering also has a separate total limit of config.start_timeout. A stuck
    native operation cannot be forcibly interrupted: its
    worker retains ownership and this instance cannot reopen another camera.
    """

    CLEANUP_TIMEOUT = 3.0

    def __init__(
        self,
        config: CaptureConfig,
        settings: CameraSettings,
        *,
        camera_factory: Callable[[], Any] | None = None,
        available_memory_bytes: Callable[[], int | None] | None = None,
        sensor_clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self.config, self.settings = config, settings
        # Reuse the existing native-mode, controls and RAM validation. The full
        # ring capacity is budgeted even when the first operation is a single.
        self._preparer = Picamera2FrameCapture(
            config, settings, camera_factory=camera_factory,
            available_memory_bytes=available_memory_bytes)
        self._sensor_clock_ns = sensor_clock_ns or now_sensor_clock_ns
        self._condition = threading.Condition(threading.RLock())
        self._worker: threading.Thread | None = None
        self._active: _Operation | None = None
        self._generation = 0
        self._last_capture: dict[str, Any] | None = None
        self._state = "idle"
        self._closed = False
        self._shutdown = False
        self._finished = False
        self._error: str | None = None
        self._cleanup_error: str | None = None
        self._cleanup_timeout_error: str | None = None
        self._shutdown_at: float | None = None
        self._startup_deadline: float | None = None
        self._camera_started = False
        self._camera_open_count = 0
        self._camera_start_count = 0
        self._camera_stop_count = 0
        self._camera_close_count = 0
        self._copied_frames_total = 0
        self._last_timestamp = -1

    def _ensure_available_locked(self) -> None:
        if self._closed:
            raise FrameUnavailable("closed", "The keep-alive camera session is closed")
        if self._cleanup_error or self._cleanup_timeout_error:
            raise FrameUnavailable("cleanup_failed", self._cleanup_error or self._cleanup_timeout_error)
        if self._error:
            raise FrameUnavailable("capture_failed", self._error)
        if self._active is not None:
            raise FrameUnavailable("busy", "Another camera operation is active or still pausing")

    def _begin(self, mode: str, max_duration: float | None) -> _Operation:
        with self._condition:
            self._ensure_available_locked()
            self._generation += 1
            operation = _Operation(
                self._generation, mode,
                FrameRingBuffer(1 if mode == "single" else self.config.capacity),
                max_duration)
            self._active = operation
            self._state = "starting"
            if self._worker is None:
                self._startup_deadline = time.monotonic() + self.config.start_timeout
                self._worker = threading.Thread(
                    target=self._run, name="keep-alive-frame-capture", daemon=True)
                self._worker.start()
                threading.Thread(target=self._watchdog,
                                 name="keep-alive-frame-limits", daemon=True).start()
            self._condition.notify_all()
            while (operation.started_at is None and not operation.done and not self._error
                   and not self._closed and operation.stop_reason is None):
                self._condition.wait()
            if self._error:
                raise FrameUnavailable("capture_failed", self._error)
            if self._closed and not (operation.done and operation.stop_reason == "single_complete"):
                raise FrameUnavailable("closed", "The camera session was closed during startup")
            if operation.started_at is None:
                raise FrameUnavailable("stopped", "Camera operation was interrupted before startup")
            return operation

    def capture_one(self) -> FrameRecord:
        """Copy one fresh frame; keep the configured camera streaming."""
        operation = self._begin("single", None)
        with self._condition:
            while not operation.done and not self._error and not self._closed:
                self._condition.wait()
            if self._error:
                raise FrameUnavailable("capture_failed", self._error)
            if not operation.done and self._closed:
                raise FrameUnavailable("closed", "The camera session was closed during single capture")
            result, operation.result = operation.result, None
            if result is None or operation.stop_reason != "single_complete":
                raise FrameUnavailable("stopped", "Single-frame capture was interrupted")
            return result

    def start_continuous(self, max_duration: float = 6.0) -> "KeepAliveFrameCapture":
        """Start a new ring, automatically pausing application reads at its limit."""
        self._begin("continuous", _positive_number(max_duration, "max_duration"))
        return self

    def get_before(
        self, timestamp_ns: int, timeout: float = 2.0, max_age_ns: int | None = None
    ) -> FrameRecord:
        target = _nanoseconds(timestamp_ns)
        if max_age_ns is not None:
            _nanoseconds(max_age_ns, "max_age_ns")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be non-negative and finite")
        deadline = time.monotonic() + timeout
        with self._condition:
            operation = self._active
            while True:
                if self._error:
                    raise FrameUnavailable("capture_failed", self._error)
                if (operation is None or self._active is not operation or
                        operation.mode != "continuous" or operation.state != "running"):
                    raise FrameUnavailable("stopped", "Continuous capture is not running")
                latest = operation.buffer.stats()["latest_timestamp_ns"]
                if latest is not None and latest >= target:
                    return operation.buffer.get_before(target, max_age_ns)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FrameUnavailable("timeout", "No frame at or after the target arrived before timeout")
                self._condition.wait(remaining)

    def _pause_locked(self, operation: _Operation, reason: str) -> None:
        if operation.stop_reason is None:
            operation.stop_reason = reason
            operation.stop_requested_at = time.monotonic()
        operation.state = "stopping"
        if not self._error:
            self._state = "stopping"
        self._condition.notify_all()

    def stop_continuous(self, timeout: float = 3.0) -> None:
        """Drain the pending request and discard the ring, without stopping hardware."""
        timeout = _positive_number(timeout, "timeout")
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._cleanup_error or self._cleanup_timeout_error:
                raise FrameUnavailable("cleanup_failed", self._cleanup_error or self._cleanup_timeout_error)
            if self._error:
                raise FrameUnavailable("capture_failed", self._error)
            operation = self._active
            if operation is None:
                return
            if operation.mode != "continuous":
                raise FrameUnavailable("busy", "A single-frame capture is still active")
            self._pause_locked(operation, "explicit_stop")
            while not operation.done and not self._error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail_locked("Camera pause timed out; the worker still owns the camera")
                    raise FrameUnavailable("timeout", self._error)
                self._condition.wait(remaining)
            if self._error:
                raise FrameUnavailable("capture_failed", self._error)

    def _fail_locked(self, message: str) -> None:
        if self._error is None:
            self._error = message
        self._shutdown = True
        self._shutdown_at = self._shutdown_at or time.monotonic()
        self._state = "failed"
        if self._active is not None:
            self._pause_locked(self._active, "capture_failed")
        self._condition.notify_all()

    def close(self, timeout: float = 3.0) -> None:
        """Permanently stop and close hardware; repeated close waits for its owner."""
        timeout = _positive_number(timeout, "timeout")
        with self._condition:
            self._closed = self._shutdown = True
            self._shutdown_at = self._shutdown_at or time.monotonic()
            if self._active is not None:
                self._pause_locked(self._active, "closed")
            if self._worker is None:
                self._finished = True
                self._state = "closed"
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout)
            if self._worker.is_alive():
                with self._condition:
                    self._cleanup_timeout_error = (
                        "Camera cleanup timed out; the worker still owns the camera. "
                        "Do not create another camera session until the process exits.")
                raise FrameUnavailable("timeout", self._cleanup_timeout_error)
        if self._cleanup_error:
            raise FrameUnavailable("cleanup_failed", self._cleanup_error)

    def _operation_stats_locked(self, operation: _Operation) -> dict[str, Any]:
        details = self._preparer.stats()
        details.update(operation.buffer.stats())
        details.update(
            mode=operation.mode, state=operation.state, error=self._error,
            cleanup_error=self._cleanup_error or self._cleanup_timeout_error,
            stop_reason=operation.stop_reason, started_monotonic=operation.started_at,
            stop_requested_monotonic=operation.stop_requested_at,
            max_duration_seconds=operation.max_duration,
            duplicates_dropped=operation.duplicates,
            fresh_after_timestamp_ns=operation.fresh_after_ns,
            fresh_frame_received=operation.fresh_ready,
            discarded_before_activation=operation.discarded_before_activation,
            fresh_start_timeout_seconds=self.config.start_timeout,
            worker_alive=self._worker is not None and self._worker.is_alive())
        return details

    def stats(self) -> dict[str, Any]:
        with self._condition:
            current = (self._operation_stats_locked(self._active)
                       if self._active is not None else None)
            last = deepcopy(self._last_capture)
            details = current or last or {}
            return {
                "mode": self._active.mode if self._active is not None else "idle",
                "state": ("cleanup_failed" if self._cleanup_error or self._cleanup_timeout_error
                          else self._state),
                "stop_reason": details.get("stop_reason"),
                "error": self._error,
                "cleanup_error": self._cleanup_error or self._cleanup_timeout_error,
                "current_capture": current, "last_capture": last,
                "camera_started": self._camera_started,
                "camera_open_count": self._camera_open_count,
                "camera_start_count": self._camera_start_count,
                "camera_stop_count": self._camera_stop_count,
                "camera_close_count": self._camera_close_count,
                "copied_frames_total": self._copied_frames_total,
                "cached_image_bytes": current["image_bytes"] if current else 0,
            }

    def _finish_locked(self, operation: _Operation) -> None:
        operation.state = "failed" if self._error else "stopped"
        self._last_capture = self._operation_stats_locked(operation)
        operation.buffer = FrameRingBuffer(operation.buffer.capacity)
        operation.done = True
        if self._active is operation:
            self._active = None
        self._state = "failed" if self._error else "closed" if self._closed else "idle"
        self._condition.notify_all()

    def _read_window(self, camera: Any, operation: _Operation) -> None:
        sequence = 0
        pending = None
        try:
            while True:
                with self._condition:
                    if operation.stop_reason is not None or self._shutdown:
                        return
                # Inspect each completion ourselves. Picamera2's flush job
                # hides discarded frames, which would make a 200 ms job wait
                # look like a sensor stall while fresh exposure is preparing.
                pending = camera.capture_request(wait=False)
                while True:
                    try:
                        request = pending.get_result(timeout=0.025)
                        pending = None
                        break
                    except JobTimeout:
                        with self._condition:
                            if self._shutdown:
                                return
                try:
                    with self._condition:
                        if operation.stop_reason is not None or self._shutdown:
                            return
                    metadata = request.get_metadata()
                    stamp = _nanoseconds(metadata["SensorTimestamp"])
                    if stamp < self._last_timestamp:
                        raise RuntimeError("Camera SensorTimestamp went backwards")
                    if stamp == self._last_timestamp:
                        with self._condition:
                            operation.duplicates += 1
                        continue
                    exposure_ns = _nanoseconds(metadata["ExposureTime"], "ExposureTime") * 1000
                    with self._condition:
                        if operation.stop_reason is not None or self._shutdown:
                            return
                        self._last_timestamp = stamp
                        operation.progress_at = time.monotonic()
                        self._condition.notify_all()
                        if (not operation.fresh_ready and
                                stamp - exposure_ns < operation.fresh_after_ns):
                            operation.discarded_before_activation += 1
                            continue
                    array = request.make_array("main")
                    with self._condition:
                        self._copied_frames_total += 1
                    if array.nbytes > self._preparer._negotiated["main"]["framesize"]:
                        raise MemoryError("Camera image exceeds its negotiated memory size")
                    record = FrameRecord(sequence, stamp, array, metadata)
                finally:
                    request.release()
                with self._condition:
                    if operation.stop_reason is not None or self._shutdown:
                        return
                    operation.buffer.append(record)
                    operation.fresh_ready = True
                    sequence += 1
                    if operation.mode == "single":
                        operation.result = record
                        self._pause_locked(operation, "single_complete")
                    self._condition.notify_all()
        finally:
            if pending is not None:
                # Only a fatal/close path can leave an unfinished request. A
                # normal pause waits for it, then releases without make_array.
                Picamera2FrameCapture._release_pending(camera, pending)

    def _run(self) -> None:
        camera = None
        try:
            camera = self._preparer._make_camera()
            with self._condition:
                self._camera_open_count += 1
            self._preparer._prepare(camera)
            with self._condition:
                if self._shutdown:
                    return
            camera.start()
            with self._condition:
                self._camera_start_count += 1
                self._camera_started = True
                self._startup_deadline = None
                self._condition.notify_all()
            while True:
                with self._condition:
                    while self._active is None and not self._shutdown:
                        self._condition.wait()
                    if self._shutdown:
                        return
                    operation = self._active
                    if operation.stop_reason is not None:
                        self._finish_locked(operation)
                        del operation
                        continue
                    operation.fresh_after_ns = _nanoseconds(self._sensor_clock_ns())
                    operation.started_at = operation.progress_at = time.monotonic()
                    operation.state = self._state = "running"
                    self._condition.notify_all()
                self._read_window(camera, operation)
                with self._condition:
                    self._finish_locked(operation)
                # Do not retain single images or an old ring on the idle stack.
                del operation
        except Exception as exc:
            with self._condition:
                self._fail_locked(f"{type(exc).__name__}: {exc}")
        finally:
            if camera is not None:
                for name, cleanup in (("stop", camera.stop), ("close", camera.close)):
                    try:
                        with self._condition:
                            if name == "stop":
                                self._camera_stop_count += 1
                            else:
                                self._camera_close_count += 1
                        cleanup()
                        if name == "stop":
                            with self._condition:
                                self._camera_started = False
                    except Exception as exc:
                        with self._condition:
                            self._cleanup_error = (
                                f"Camera {name} failed: {type(exc).__name__}: {exc}")
            with self._condition:
                if self._active is not None:
                    self._finish_locked(self._active)
                self._finished = True
                self._cleanup_timeout_error = None
                self._state = "failed" if self._error else "closed"
                self._condition.notify_all()

    def _watchdog(self) -> None:
        with self._condition:
            while not self._finished:
                now = time.monotonic()
                if self._shutdown:
                    deadline = self._shutdown_at + self.CLEANUP_TIMEOUT
                    if now >= deadline:
                        self._cleanup_timeout_error = (
                            "Camera cleanup timed out; the worker still owns the camera")
                        self._condition.notify_all()
                        return
                    self._condition.wait(deadline - now)
                    continue
                if self._startup_deadline is not None:
                    if now >= self._startup_deadline:
                        self._fail_locked("Camera startup timed out")
                        continue
                    self._condition.wait(self._startup_deadline - now)
                    continue
                operation = self._active
                if operation is None or operation.started_at is None:
                    self._condition.wait()
                    continue
                frame_deadline = operation.progress_at + self.config.frame_timeout
                freshness_deadline = (None if operation.fresh_ready else
                                      operation.started_at + self.config.start_timeout)
                duration_deadline = (operation.started_at + operation.max_duration
                                     if operation.max_duration is not None else None)
                if now >= frame_deadline:
                    self._fail_locked("Camera frame acquisition timed out")
                    continue
                if (operation.stop_reason is None and freshness_deadline is not None
                        and now >= freshness_deadline):
                    self._fail_locked("Fresh-frame preparation timed out")
                    continue
                if (operation.stop_reason is None and duration_deadline is not None
                        and now >= duration_deadline):
                    self._pause_locked(operation, "duration_limit")
                next_deadline = frame_deadline
                if operation.stop_reason is None and freshness_deadline is not None:
                    next_deadline = min(next_deadline, freshness_deadline)
                if operation.stop_reason is None and duration_deadline is not None:
                    next_deadline = min(next_deadline, duration_deadline)
                self._condition.wait(max(0.0, next_deadline - now))

    def __enter__(self) -> "KeepAliveFrameCapture":
        with self._condition:
            if self._closed:
                raise FrameUnavailable("closed", "The keep-alive camera session is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"Camera cleanup also failed: {cleanup_error}")
