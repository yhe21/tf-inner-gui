"""Bounded, process-isolated supervision of the Raspberry Pi camera service.

No Qt or Picamera2 objects cross this boundary. A blocked libcamera call can
therefore be stopped without killing the GUI or reusing an abandoned request.
"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import multiprocessing
from pathlib import Path
import platform
import queue
import subprocess
import tempfile
import time
from typing import Callable, Optional


@dataclass(frozen=True)
class RecoveryLimits:
    startup_timeout: float = 30.0
    capture_timeout: float = 5.0
    processing_timeout: float = 30.0
    calibration_timeout: float = 10.0
    health_interval: float = 5.0
    retry_delay: float = 3.0
    initial_retry_delay: float = 10.0
    max_retries: int = 10
    shutdown_timeout: float = 1.0
    poll_interval: float = 0.05

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")


def request_pi_reboot() -> str:
    """Request one normal Pi reboot, with a persistent 30-minute loop guard.

    The marker is saved before running the command. Even an ambiguous timeout
    cannot cause the GUI on the next boot to immediately request another reboot.
    Failures are raised for the caller to display; they are never silently
    interpreted as a successful reboot.
    """
    if platform.system() != "Linux":
        raise RuntimeError("Automatic reboot is only available on Raspberry Pi")
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00\n")
    except OSError as error:
        raise RuntimeError("Unable to verify Raspberry Pi hardware") from error
    if "Raspberry Pi" not in model:
        raise RuntimeError("Automatic reboot is only available on Raspberry Pi")

    marker = Path.home() / ".local/state/tf_inner/camera_reboot.json"
    now = time.time()
    if marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            elapsed = now - float(previous["requested_at"])
            if not math.isfinite(elapsed):
                raise ValueError("Invalid reboot timestamp")
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RuntimeError("Camera reboot guard could not be read; reboot blocked") from error
        if elapsed < 30 * 60:
            raise RuntimeError(
                "Camera reboot blocked: another automatic reboot was requested "
                "within the last 30 minutes"
            )

    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=marker.parent,
            prefix=".camera-reboot-", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"requested_at": now, "reason": "camera_recovery_exhausted"}, stream)
            stream.flush()
        temporary.chmod(0o600)
        temporary.replace(marker)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "reboot"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Raspberry Pi reboot request failed: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "permission denied").strip()
        raise RuntimeError(f"Raspberry Pi reboot request failed: {detail}")
    return "Raspberry Pi reboot requested after camera recovery failed"


class _CameraUnavailable(Exception):
    pass


class CameraProcessSupervisor:
    """Own one child at a time and report its events from the caller's thread.

    The target must send ``(signal_name, args_tuple)`` and accept the existing
    CaptureWorker command tuples. Its ready signal must follow a real frame
    probe. Only operations made after that probe can lead to automatic reboot.
    """

    def __init__(
        self,
        commands: queue.Queue,
        emit: Callable[[str, tuple], None],
        settings: Optional[dict],
        model_root: Optional[str],
        target: Callable,
        limits: Optional[RecoveryLimits] = None,
        reboot: Callable = request_pi_reboot,
        context=None,
    ) -> None:
        self.commands = commands
        self.emit = emit
        self.settings = deepcopy(settings)
        self.model_root = model_root
        self.target = target
        self.limits = limits or RecoveryLimits()
        self.reboot = reboot
        self.context = context or multiprocessing.get_context("spawn")
        self.ever_ready = False
        self.reconnect_attempts = 0
        self._stopping = False
        self._pending = deque()
        self._active = None
        self._active_deadline = 0.0
        self._active_phase = ""
        self._process = None
        self._connection = None
        self._child_ready = False
        self._startup_probe_ok = False
        self._last_health = 0.0
        self._reboot_requested = False

    def run(self) -> None:
        try:
            recovering = False
            while not self._stopping:
                self._collect_commands()
                if self._stopping:
                    break
                if recovering and self.ever_ready:
                    if self.reconnect_attempts >= self.limits.max_retries:
                        self._request_reboot()
                        break
                    self.reconnect_attempts += 1
                    self.emit("unavailable", (
                        f"Reconnecting camera ({self.reconnect_attempts}/"
                        f"{self.limits.max_retries})...",
                    ))
                # A child in uninterruptible kernel I/O may outlive SIGKILL.
                # Reclaim it before trying to open another camera instance.
                if self._process is not None and not self._close_child():
                    cleanup_failure = "Camera process could not be stopped; retrying cleanup"
                    self.emit("unavailable", (cleanup_failure,))
                    self._wait_for_retry(
                        self.limits.retry_delay if self.ever_ready
                        else self.limits.initial_retry_delay,
                        cleanup_failure,
                    )
                    continue
                failure = None
                try:
                    self._start_child()
                    self._serve_child()
                except Exception as error:
                    failure = str(error) or "Camera process disconnected"
                if self._stopping:
                    break
                if failure is None:
                    failure = "Camera process stopped unexpectedly"
                self._report_failure(failure)
                if not self._close_child():
                    # The first failed cleanup after a working camera is the
                    # first recovery attempt. Later iterations count above.
                    if self.ever_ready and self.reconnect_attempts == 0:
                        self.reconnect_attempts = 1
                    self.emit("unavailable", (
                        "Camera process could not be stopped; retrying cleanup",
                    ))
                # A healthy ready event resets this count inside _serve_child.
                recovering = True
                delay = (
                    self.limits.retry_delay if self.ever_ready
                    else self.limits.initial_retry_delay
                )
                self._wait_for_retry(delay, failure)
        finally:
            if self._stopping:
                self.emit("unavailable", ("Camera stopped",))
            if not self._reboot_requested and not self._close_child():
                self.emit("unavailable", (
                    "Camera process remains blocked in kernel I/O after termination; "
                    "application exit may wait for a manual Raspberry Pi restart",
                ))
            self.emit("stopped", ())

    def _start_child(self) -> None:
        self._child_ready = False
        self._startup_probe_ok = False
        self._active = None
        parent_connection, child_connection = self.context.Pipe(duplex=True)
        self._connection = parent_connection
        self._process = self.context.Process(
            target=self.target,
            args=(child_connection, {
                "settings": deepcopy(self.settings),
                "model_root": self.model_root,
            }),
            daemon=True,
        )
        try:
            self._process.start()
        except Exception:
            parent_connection.close()
            self._connection = None
            self._process = None
            raise
        finally:
            child_connection.close()

    def _collect_commands(self) -> None:
        # A producer cannot keep this loop from checking the child deadline.
        for _ in range(256):
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                break
            if command is None:
                self._stopping = True
                self._pending.clear()
                return
            self._pending.append(command)

    def _serve_child(self) -> None:
        startup_deadline = time.monotonic() + self.limits.startup_timeout
        while not self._stopping:
            self._collect_commands()
            if self._stopping:
                return

            # Consume already-arrived completions before checking deadlines.
            if self._connection.poll(self.limits.poll_interval):
                for _ in range(256):
                    name, args = self._connection.recv()
                    self._handle_event(name, tuple(args))
                    if not self._connection.poll(0):
                        break

            if not self._process.is_alive():
                raise _CameraUnavailable("Camera process exited unexpectedly")
            now = time.monotonic()
            if not self._child_ready:
                if now >= startup_deadline:
                    if self._startup_probe_ok:
                        raise _CameraUnavailable(
                            "AI initialization timed out after a successful camera probe"
                        )
                    raise _CameraUnavailable("Camera initialization timed out")
                continue
            if self._active is not None:
                if now >= self._active_deadline:
                    raise _CameraUnavailable(f"Camera {self._active_phase} timed out")
                continue
            if self._pending:
                self._start_operation(self._pending.popleft())
            elif now - self._last_health >= self.limits.health_interval:
                self._start_operation(("health_check", None, False, None, False))

    def _start_operation(self, command) -> None:
        name = command[0]
        if name == "capture":
            timeout = self.limits.capture_timeout
            phase = "frame acquisition"
        elif name == "auto_calibrate":
            timeout = self.limits.calibration_timeout
            phase = "calibration"
        elif name == "health_check":
            timeout = self.limits.capture_timeout
            phase = "health check"
        else:
            self.emit("failed", (f"Unknown camera command: {name}",))
            return
        self._active = command
        self._active_phase = phase
        self._active_deadline = time.monotonic() + timeout
        self._connection.send(command)

    def _handle_event(self, name: str, args: tuple) -> None:
        if name in {"camera_fault", "initialization_failed"}:
            raise _CameraUnavailable(str(args[0]) if args else "Camera unavailable")
        if name == "stopped":
            raise _CameraUnavailable("Camera process stopped unexpectedly")
        if name == "ready":
            self._child_ready = True
            self.ever_ready = True
            self.reconnect_attempts = 0
            self._last_health = time.monotonic()
        elif name == "health_checked":
            if not self._child_ready:
                # The child probes before loading AI. Repeated AI load hangs
                # must not accumulate camera failures and reboot healthy hardware.
                self._startup_probe_ok = True
                self.reconnect_attempts = 0
                self._last_health = time.monotonic()
            if self._active is not None and self._active[0] == "health_check":
                self._active = None
                self._last_health = time.monotonic()
            return
        elif name == "frame_acquired":
            if self._active is None or self._active[0] != "capture":
                return
            self._active_phase = "image processing"
            self._active_deadline = time.monotonic() + self.limits.processing_timeout
            self._last_health = time.monotonic()
        elif name in {"succeeded", "failed"}:
            if self._active is None or self._active[0] != "capture":
                return
            self._active = None
        elif name in {"auto_calibration_succeeded", "auto_calibration_failed"}:
            if self._active is None or self._active[0] != "auto_calibrate":
                return
            self._active = None
            self.settings = deepcopy(args[0]) if name.endswith("succeeded") else None
            if name.endswith("succeeded"):
                self._last_health = time.monotonic()
        elif name in {"inspection_completed", "inspection_failed", "capture_started"}:
            if self._active is None or self._active[0] != "capture":
                return
        self.emit(name, args)

    def _fail_command(self, command, reason: str) -> None:
        if command[0] == "capture":
            self.emit("failed", (reason,))
        elif command[0] == "auto_calibrate":
            self.emit("auto_calibration_failed", (reason,))

    def _report_failure(self, reason: str) -> None:
        self._child_ready = False
        self.emit("unavailable", (f"Camera unavailable: {reason}",))
        if self._active is not None:
            self._fail_command(self._active, reason)
            self._active = None
        self._discard_pending(reason)

    def _discard_pending(self, reason: str) -> None:
        self._collect_commands()
        while self._pending:
            self._fail_command(self._pending.popleft(), reason)

    def _wait_for_retry(self, delay: float, reason: str) -> None:
        deadline = time.monotonic() + delay
        while not self._stopping:
            self._discard_pending(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self.limits.poll_interval, remaining))

    def _close_child(self) -> bool:
        process, connection = self._process, self._connection
        if process is None:
            if connection is not None:
                connection.close()
                self._connection = None
            return True
        try:
            if process.is_alive():
                # A graceful request is best effort; never wait for the camera's
                # own stop/close calls, which can block along with capture.
                if connection is not None:
                    try:
                        connection.send(None)
                    except (OSError, EOFError, BrokenPipeError):
                        pass
                if self._stopping:
                    process.join(min(self.limits.shutdown_timeout, 0.2))
                if process.is_alive():
                    process.terminate()
                    process.join(self.limits.shutdown_timeout)
            if process.is_alive():
                process.kill()
                process.join(self.limits.shutdown_timeout)
            if process.is_alive():
                return False
            process.join(0)
            process.close()
            self._process = None
            return True
        finally:
            if connection is not None:
                connection.close()
                self._connection = None

    def _request_reboot(self) -> None:
        if self._reboot_requested:
            return
        self._reboot_requested = True
        self.emit("unavailable", (
            f"Camera unavailable after {self.limits.max_retries} recovery attempts; "
            "requesting Raspberry Pi restart...",
        ))
        try:
            outcome = self.reboot()
            if outcome is False:
                raise RuntimeError("Raspberry Pi restart request was declined")
        except Exception as error:
            self.emit("unavailable", (f"Automatic restart failed: {error}",))
        else:
            self.emit("unavailable", (str(outcome or "Raspberry Pi restart requested"),))
