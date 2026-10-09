"""Optional INNER -> delayed buffer -> GLUE workflow, without GUI dependencies."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import threading

from .buffer import FrameRecord, FrameUnavailable, _nanoseconds


@dataclass(frozen=True)
class TriggerConfig:
    enabled: bool = True
    start_delay: float = 3.0
    max_duration: float = 6.0
    query_timeout: float = 2.0

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a bool")
        for name in ("start_delay", "max_duration", "query_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value < 0 or (name != "start_delay" and value == 0):
                raise ValueError(f"{name} must be {'non-negative' if name == 'start_delay' else 'positive'}")


class TriggeredFrameCapture:
    """Return INNER's single image and arm a cancellable delayed GLUE window.

    on_inner returns the image to the caller for inference. This class does not
    run a model. With enabled=False both events return newly captured images;
    only enabled=True provides historical, strictly-before-GLUE selection.
    """

    def __init__(self, session, config=None, *, timer_factory=threading.Timer):
        self.session = session
        self.config = config or TriggerConfig()
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._operation = threading.RLock()
        self._timer = None
        self._generation = 0
        self._state = "idle"
        self._error = None
        self._closed = False

    def _cancel_locked(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        return self._generation

    def _ensure_open_locked(self):
        if self._closed:
            raise FrameUnavailable("closed", "Trigger workflow is closed")

    def _ensure_current(self, generation):
        with self._lock:
            self._ensure_open_locked()
            if generation != self._generation:
                raise FrameUnavailable("superseded", "A newer event cancelled this operation")

    def _finish_state(self, generation, state, error=None):
        with self._lock:
            if generation == self._generation and not self._closed:
                self._state = state
                self._error = None if error is None else f"{type(error).__name__}: {error}"

    def on_inner(self) -> FrameRecord:
        with self._lock:
            self._ensure_open_locked()
            generation = self._cancel_locked()
            self._state, self._error = "capturing_inner", None
        with self._operation:
            self._ensure_current(generation)
            try:
                self.session.stop_continuous()
                frame = self.session.capture_one()
                with self._lock:
                    # A concurrent GLUE, switch change or close may cancel the
                    # next window while this single exposure finishes.
                    if generation == self._generation and not self._closed:
                        if self.config.enabled:
                            self._state = "waiting"
                            self._timer = self._timer_factory(
                                self.config.start_delay, self._start_buffer,
                                args=(generation,))
                            self._timer.daemon = True
                            self._timer.start()
                        else:
                            self._state = "idle"
                return frame
            except BaseException as error:
                self._finish_state(generation, "failed", error)
                raise

    def _start_buffer(self, generation):
        with self._operation:
            with self._lock:
                if (self._closed or generation != self._generation or
                        not self.config.enabled or self._state != "waiting"):
                    return
                self._timer = None
                self._state = "starting"
            try:
                self.session.start_continuous(max_duration=self.config.max_duration)
                with self._lock:
                    current = generation == self._generation and not self._closed
                    if current:
                        self._state = "buffering"
                if not current:
                    # Cancellation can happen during a slow native startup.
                    self.session.stop_continuous()
            except Exception as error:
                self._finish_state(generation, "failed", error)

    def on_glue(self, timestamp_ns: int) -> FrameRecord:
        timestamp_ns = _nanoseconds(timestamp_ns)
        with self._lock:
            self._ensure_open_locked()
            previous_state = self._state
            generation = self._cancel_locked()
            enabled = self.config.enabled
            self._state = "finishing"
        with self._operation:
            self._ensure_current(generation)
            error = None
            frame = None
            try:
                if enabled:
                    if previous_state != "buffering":
                        raise FrameUnavailable(
                            "not_ready", "GLUE arrived without a running buffer window")
                    frame = self.session.get_before(timestamp_ns, timeout=self.config.query_timeout)
                else:
                    frame = self.session.capture_one()
            except BaseException as exc:
                error = exc
            finally:
                try:
                    self.session.stop_continuous()
                    stopped = self.session.stats()
                    if stopped.get("error") or stopped.get("cleanup_error"):
                        raise FrameUnavailable("capture_failed", str(
                            stopped.get("error") or stopped.get("cleanup_error")))
                except Exception as cleanup_error:
                    if error is None:
                        error = cleanup_error
                    elif hasattr(error, "add_note"):
                        error.add_note(f"Camera cleanup also failed: {cleanup_error}")
                self._finish_state(generation, "idle" if error is None else "failed", error)
            if error is not None:
                raise error
            return frame

    def set_enabled(self, enabled: bool):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")
        with self._lock:
            self._ensure_open_locked()
            if enabled == self.config.enabled:
                return
            generation = self._cancel_locked()
            self.config = replace(self.config, enabled=enabled)
            self._state = "finishing"
        with self._operation:
            self._ensure_current(generation)
            try:
                self.session.stop_continuous()
            except Exception as error:
                self._finish_state(generation, "failed", error)
                raise
            self._finish_state(generation, "idle")

    def stats(self):
        with self._lock:
            result = {
                "state": self._state, "enabled": self.config.enabled,
                "start_delay": self.config.start_delay,
                "max_duration": self.config.max_duration,
                "error": self._error,
            }
        result["session"] = self.session.stats()
        if result["state"] == "buffering" and result["session"].get("state") in {"idle", "cleanup_failed"}:
            result["state"] = ("window_closed" if result["session"].get("stop_reason") == "duration_limit"
                               else "failed")
        return result

    def close(self):
        with self._lock:
            if not self._closed:
                self._cancel_locked()
                self._closed = True
                self._state = "closed"
        with self._operation:
            # Repeated close retries a previously timed-out cleanup.
            self.session.close()

    def __enter__(self):
        with self._lock:
            self._ensure_open_locked()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"Camera cleanup also failed: {cleanup_error}")
