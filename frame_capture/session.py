"""Reusable on-demand ownership of short, fully closed camera sessions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import threading
import time
from typing import Any, Callable

from .buffer import FrameRecord, FrameUnavailable
from .capture import CameraSettings, CaptureConfig, Picamera2FrameCapture, _positive_number


class OnDemandFrameCapture:
    """Capture one frame, or run a bounded continuous capture window.

    Each operation creates a fresh camera object. Single capture copies exactly
    one completed request and returns only after stop/close succeeds. Native
    camera startup may itself expose more than one sensor frame internally.

    A continuous window stops automatically after ``max_duration`` seconds,
    measured from successful camera.start(), regardless of caller polling. A
    returned frame owns immutable bytes and survives all following sessions.
    Failure to finish cleanup retains ownership and prevents a second camera
    session; a native cleanup exception permanently blocks this instance.
    """

    CLEANUP_TIMEOUT = 3.0

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
        self._camera_factory = camera_factory
        self._memory_probe = available_memory_bytes
        self._lock = threading.RLock()
        self._active: Picamera2FrameCapture | None = None
        self._mode = "idle"
        self._last_capture: dict[str, Any] | None = None
        self._cleanup_timeout_error: str | None = None
        self._unsafe_error: str | None = None
        self._closed = False

    def _reap_locked(self) -> None:
        capture = self._active
        if capture is None or not capture._finished.is_set():
            return
        if capture._thread is not None and capture._thread.is_alive():
            return
        result = capture.stats()
        if self._cleanup_timeout_error:
            result["cleanup_timeout_observed"] = self._cleanup_timeout_error
        result["mode"] = self._mode
        self._last_capture = result
        if result["cleanup_error"] is not None:
            self._unsafe_error = result["cleanup_error"]
        self._active = None
        self._mode = "idle"
        self._cleanup_timeout_error = None
        # Dropping the capture drops the ring. Last-session statistics retain
        # counts and timings only, never the ~1 GiB native-resolution history.

    def _begin(self, mode: str, max_duration: float | None) -> Picamera2FrameCapture:
        with self._lock:
            self._reap_locked()
            if self._closed:
                raise FrameUnavailable("closed", "The on-demand camera session is closed")
            if self._unsafe_error is not None:
                raise FrameUnavailable("cleanup_failed", self._unsafe_error)
            if self._active is not None:
                raise FrameUnavailable("busy", "Another camera operation is active or still cleaning up")
            config = replace(self.config, capacity=1) if mode == "single" else self.config
            capture = Picamera2FrameCapture(
                config, self.settings,
                camera_factory=self._camera_factory,
                available_memory_bytes=self._memory_probe,
                _frame_limit=1 if mode == "single" else None,
                _max_duration=max_duration,
                _watch_limits=True,
            )
            self._active = capture
            self._mode = mode
            self._cleanup_timeout_error = None
            threading.Thread(target=self._supervise, args=(capture,),
                             name="on-demand-camera-cleanup", daemon=True).start()
        try:
            capture.start()
        except BaseException as exc:
            capture._request_stop("capture_failed")
            try:
                self._stop_capture(capture, self.CLEANUP_TIMEOUT)
            except Exception as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Camera cleanup also failed: {cleanup_error}")
            raise
        return capture

    def _mark_cleanup_timeout(self, capture: Picamera2FrameCapture) -> str:
        message = ("Camera cleanup timed out; the worker still owns the camera. "
                   "A new capture cannot start until that worker finishes cleanup.")
        with self._lock:
            if self._active is capture:
                self._cleanup_timeout_error = message
        return message

    def _supervise(self, capture: Picamera2FrameCapture) -> None:
        # The worker/watchdog sets this for explicit stop, frame failure, single
        # completion, or duration limit. Never call a camera native API here.
        capture._stop.wait()
        requested = capture._stop_requested_at or time.monotonic()
        remaining = max(0.0, requested + self.CLEANUP_TIMEOUT - time.monotonic())
        if not capture._finished.wait(remaining):
            self._mark_cleanup_timeout(capture)
        capture._finished.wait()
        if capture._thread is not None:
            capture._thread.join()
        with self._lock:
            if self._active is capture:
                self._reap_locked()

    def _stop_capture(self, capture: Picamera2FrameCapture, timeout: float) -> None:
        try:
            capture.stop(timeout)
        except FrameUnavailable as exc:
            if exc.reason == "timeout":
                self._mark_cleanup_timeout(capture)
            raise
        finally:
            with self._lock:
                self._reap_locked()

    def capture_one(self) -> FrameRecord:
        """Read/copy one request and return it after the device is fully closed."""
        capture = self._begin("single", None)
        # A watchdog bounds even native request/image calls that ignore a job
        # timeout; the supervisor separately bounds the ensuing cleanup.
        if not capture._finished.wait(self.config.frame_timeout + self.CLEANUP_TIMEOUT):
            capture._request_stop("capture_failed")
            raise FrameUnavailable("timeout", self._mark_cleanup_timeout(capture))
        self._stop_capture(capture, self.CLEANUP_TIMEOUT)
        status = capture.stats()
        if status["error"] is not None:
            raise FrameUnavailable("capture_failed", status["error"])
        frames = capture.snapshot()
        if len(frames) != 1 or status["stop_reason"] != "single_complete":
            raise FrameUnavailable("stopped", "Single-frame capture was interrupted before completion")
        return frames[0]

    def start_continuous(self, max_duration: float = 6.0) -> "OnDemandFrameCapture":
        """Start a fresh ring and close the camera automatically at the limit."""
        self._begin("continuous", _positive_number(max_duration, "max_duration"))
        return self

    def get_before(
        self, timestamp_ns: int, timeout: float = 2.0, max_age_ns: int | None = None
    ) -> FrameRecord:
        """Wait for a watermark, then choose the strict nearest predecessor."""
        with self._lock:
            self._reap_locked()
            capture = self._active
            if self._mode != "continuous" or capture is None:
                if self._last_capture and self._last_capture["error"]:
                    raise FrameUnavailable("capture_failed", self._last_capture["error"])
                raise FrameUnavailable("stopped", "Continuous capture is not running")
        return capture.get_before(timestamp_ns, timeout, max_age_ns)

    def stop_continuous(self, timeout: float = 3.0) -> None:
        """Stop and close the active continuous window; safe to call repeatedly."""
        timeout = _positive_number(timeout, "timeout")
        with self._lock:
            self._reap_locked()
            if self._unsafe_error is not None:
                raise FrameUnavailable("cleanup_failed", self._unsafe_error)
            capture = self._active
            if capture is None:
                return
            if self._mode != "continuous":
                raise FrameUnavailable("busy", "A single-frame capture is still active")
            capture._request_stop("explicit_stop")
        self._stop_capture(capture, timeout)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            current = self._active.stats() if self._active is not None else None
            if self._cleanup_timeout_error or self._unsafe_error:
                state = "cleanup_failed"
            elif current is not None:
                state = current["state"]
                if state in {"stopped", "failed"}:
                    state = "stopping"  # The owner has not finished its worker yet.
                elif state == "new":
                    state = "starting"
            else:
                state = "closed" if self._closed else "idle"
            detail = current or self._last_capture or {}
            return {
                "mode": self._mode,
                "state": state,
                "stop_reason": detail.get("stop_reason"),
                "error": detail.get("error"),
                "cleanup_error": (self._cleanup_timeout_error or self._unsafe_error
                                  or detail.get("cleanup_error")),
                "current_capture": current,
                "last_capture": deepcopy(self._last_capture),
            }

    def close(self, timeout: float = 3.0) -> None:
        """Permanently close the session, including an in-progress single shot."""
        timeout = _positive_number(timeout, "timeout")
        with self._lock:
            self._closed = True
            self._reap_locked()
            capture = self._active
            if capture is not None:
                capture._request_stop("closed")
            elif self._unsafe_error is not None:
                raise FrameUnavailable("cleanup_failed", self._unsafe_error)
        if capture is not None:
            self._stop_capture(capture, timeout)

    def __enter__(self) -> "OnDemandFrameCapture":
        with self._lock:
            if self._closed:
                raise FrameUnavailable("closed", "The on-demand camera session is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"Camera cleanup also failed: {cleanup_error}")
