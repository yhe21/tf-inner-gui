"""Camera recovery tests use scripted children, never camera hardware/reboots."""

from collections import deque
from dataclasses import replace
import json
from multiprocessing.connection import wait
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_recovery import (  # noqa: E402
    CameraProcessSupervisor,
    RecoveryLimits,
    WakeableQueue,
    request_pi_reboot,
)


READY = ("ready", (4056, 3040, True))
PROBE_OK = ("startup_probe_succeeded", ())
ABSENT = ("initialization_failed", ("camera absent",))
FAULT = ("camera_fault", ("no frame",))
CAPTURE = ("capture", Path("image.jpg"), True, "INNER", False)
CALIBRATE = ("auto_calibrate", None, False, None, False)
FAST = RecoveryLimits(
    startup_timeout=0.015, capture_timeout=0.008,
    processing_timeout=0.03, calibration_timeout=0.015,
    retry_delay=0.001, initial_retry_delay=0.001,
    shutdown_timeout=0.002,
)


def _unused_target(connection, config):
    raise AssertionError("Scripted context never executes camera code")


def _hanging_capture_child(connection, _config):
    """Real spawn smoke test: stand in for a native acquisition that never ends."""
    connection.send(PROBE_OK)
    connection.send(READY)
    command = connection.recv()
    if command is not None:
        connection.send(("capture_started", (str(command[1]), command[2])))
        while True:
            time.sleep(1)


class FakeConnection:
    def __init__(self):
        self.events = deque()
        self.process = None
        self.closed = False

    def poll(self, timeout=0):
        if not self.events and timeout:
            time.sleep(timeout)
        return bool(self.events)

    def recv(self):
        return self.events.popleft()

    def send(self, command):
        if self.closed:
            raise BrokenPipeError("closed")
        self.process.sent.append(command)
        if command is None:
            if self.process.plan.get("graceful_exit"):
                self.process.alive = False
            return
        replies = self.process.plan.get("commands", {}).get(command[0], [])
        self.events.extend(replies)

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, connection, plan, config):
        self.connection = connection
        self.connection.process = self
        self.plan = plan
        self.config = config
        self.alive = False
        self.sent = []
        self.terminations = 0
        self.kills = 0
        self.closed = False
        self.joins = []

    def start(self):
        self.alive = True
        self.connection.events.extend(self.plan.get("initial", []))

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminations += 1
        if not self.plan.get("ignore_terminate"):
            self.alive = False

    def kill(self):
        self.kills += 1
        if not self.plan.get("ignore_kill"):
            self.alive = False

    def join(self, timeout):
        self.joins.append(timeout)
        if self.alive:
            time.sleep(timeout)

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, plans):
        self.plans = deque(plans)
        self.processes = []
        self.connection = None

    def Pipe(self, duplex):
        assert duplex
        self.connection = FakeConnection()
        return self.connection, FakeConnection()

    def Process(self, target, args, daemon):
        assert target is _unused_target and daemon
        assert not any(process.alive for process in self.processes)
        if not self.plans:
            raise AssertionError("Unexpected additional child start")
        process = FakeProcess(self.connection, self.plans.popleft(), args[1])
        self.processes.append(process)
        return process


class SupervisorTests(unittest.TestCase):
    def run_supervisor(self, plans, on_event=None, limits=FAST, settings=None,
                       on_wait=None):
        commands = WakeableQueue()
        self.addCleanup(commands.close)
        events = []
        reboots = []
        context = FakeContext(plans)
        wait_errors = []

        def emit(name, args):
            events.append((name, args))
            if on_event is not None:
                on_event(name, args, commands, context)

        supervisor = CameraProcessSupervisor(
            commands, emit, settings, "/models", _unused_target,
            limits=limits, reboot=lambda: reboots.append(True), context=context,
        )

        def wait_for_activity(timeout, child=True):
            # Only the process/connection is scripted; command notification uses
            # the real waitable queue. No periodic sleep drives this supervisor.
            if on_wait is not None:
                try:
                    on_wait(timeout, supervisor, context)
                except AssertionError as error:
                    wait_errors.append(error)
                    supervisor._stopping = True
                    return
            connection = supervisor._connection
            process = supervisor._process
            if child and ((connection is not None and connection.events)
                          or (process is not None and not process.alive)):
                return
            if timeout is None and commands.empty():
                wait_errors.append(AssertionError(
                    "Scripted idle wait needs a command or event"
                ))
                supervisor._stopping = True
                return
            wait([commands.reader], timeout)

        with mock.patch.object(supervisor, "_wait_for_activity", wait_for_activity):
            supervisor.run()
        if wait_errors:
            raise wait_errors[0]
        return supervisor, events, reboots, context

    def test_initial_absence_retries_without_reboot(self):
        failures = []

        def emit(name, args, commands, _context):
            if name == "unavailable" and "camera absent" in args[0]:
                failures.append(True)
                if len(failures) == 3:
                    commands.put(None)

        supervisor, events, reboots, context = self.run_supervisor(
            [{"initial": [ABSENT]}] * 3, emit,
        )
        self.assertFalse(supervisor.ever_ready)
        self.assertEqual(len(context.processes), 3)
        self.assertEqual(reboots, [])
        self.assertEqual(events[-1], ("stopped", ()))

    def test_runtime_timeout_ten_failed_reconnections_one_reboot(self):
        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)

        supervisor, events, reboots, context = self.run_supervisor(
            [{"initial": [READY]}] + [{"initial": [ABSENT]}] * 10,
            emit,
        )
        self.assertEqual(len(context.processes), 11)
        self.assertEqual(supervisor.reconnect_attempts, 10)
        self.assertEqual(reboots, [True])
        self.assertEqual(sum(name == "failed" for name, _ in events), 1)
        failure_index = next(i for i, event in enumerate(events) if event[0] == "failed")
        self.assertEqual(events[failure_index - 1][0], "unavailable")
        self.assertTrue(all(not process.alive for process in context.processes))

    def test_successful_recovery_resets_consecutive_failure_count(self):
        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)

        healthy = {"initial": [READY], "commands": {"capture": [FAULT]}}
        _, events, reboots, context = self.run_supervisor(
            [healthy, {"initial": [ABSENT]}, healthy]
            + [{"initial": [ABSENT]}] * 10,
            emit,
        )
        attempts = [args[0] for name, args in events
                    if name == "unavailable" and args[0].startswith("Reconnecting")]
        self.assertEqual(len(context.processes), 13)
        self.assertIn("(1/10)", attempts[0])
        self.assertIn("(2/10)", attempts[1])
        self.assertIn("(1/10)", attempts[2])
        self.assertEqual(reboots, [True])

    def test_file_or_inspection_failure_does_not_restart_camera(self):
        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)
            elif name == "failed":
                commands.put(None)

        _, events, reboots, context = self.run_supervisor([
            {"initial": [READY], "commands": {
                "capture": [("failed", ("disk full",))],
            }},
        ], emit)
        self.assertEqual(len(context.processes), 1)
        self.assertEqual(reboots, [])
        self.assertIn(("failed", ("disk full",)), events)

    def test_calibration_settings_survive_camera_process_recovery(self):
        updated = {"exposure_time_us": 1234, "analogue_gain": 2.0,
                   "colour_gains": [1.2, 1.4]}
        ready_count = []

        def emit(name, _args, commands, _context):
            if name == "ready":
                ready_count.append(True)
                commands.put(CALIBRATE if len(ready_count) == 1 else None)
            elif name == "auto_calibration_succeeded":
                commands.put(CAPTURE)

        _, _, reboots, context = self.run_supervisor([
            {"initial": [READY], "commands": {
                "auto_calibrate": [("auto_calibration_succeeded", (updated,))],
                "capture": [FAULT],
            }},
            {"initial": [READY]},
        ], emit, settings={"old": "configuration"})
        self.assertEqual(context.processes[1].config["settings"], updated)
        self.assertEqual(reboots, [])

    def test_failed_calibration_during_recovery_preserves_last_good_settings(self):
        settings = {"exposure_time_us": 3456}
        ready_count = []

        def emit(name, _args, commands, _context):
            if name == "ready":
                ready_count.append(True)
                commands.put(CALIBRATE if len(ready_count) == 1 else None)

        _, events, _, context = self.run_supervisor([
            {"initial": [READY], "commands": {"auto_calibrate": [FAULT]}},
            {"initial": [READY]},
        ], emit, settings=settings)
        self.assertEqual(context.processes[1].config["settings"], settings)
        self.assertEqual(sum(name == "auto_calibration_failed" for name, _ in events), 1)

    def test_old_capture_and_queued_production_requests_are_not_replayed(self):
        ready_count = []

        def emit(name, _args, commands, _context):
            if name == "ready":
                ready_count.append(True)
                if len(ready_count) == 1:
                    commands.put(CAPTURE)
                    commands.put(("capture", Path("queued.jpg"), False, "GLUE", True))
                else:
                    commands.put(None)

        _, events, _, context = self.run_supervisor([
            {"initial": [READY], "commands": {"capture": [
                FAULT, ("succeeded", ("image.jpg", True)),
            ]}},
            {"initial": [READY]},
        ], emit)
        self.assertEqual(sum(name == "failed" for name, _ in events), 2)
        self.assertFalse(any(name == "succeeded" for name, _ in events))
        self.assertEqual([command for command in context.processes[0].sent if command], [CAPTURE])
        self.assertEqual([command for command in context.processes[1].sent if command], [])

    def test_ready_idle_camera_waits_indefinitely_without_acquiring_frames(self):
        waits = []

        def on_wait(timeout, supervisor, context):
            waits.append(timeout)
            self.assertTrue(supervisor._child_ready)
            self.assertIsNone(timeout)
            self.assertEqual(context.processes[0].sent, [])
            supervisor.commands.put(None)

        _, _, reboots, context = self.run_supervisor(
            [{"initial": [PROBE_OK, READY]}], on_wait=on_wait,
        )
        self.assertEqual(waits, [None])
        self.assertEqual([command for command in context.processes[0].sent
                          if command is not None], [])
        self.assertEqual(len(context.processes), 1)
        self.assertEqual(reboots, [])

    def test_already_arrived_capture_is_dispatched_before_first_wait(self):
        waits = []

        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)

        def on_wait(timeout, supervisor, context):
            waits.append(timeout)
            self.assertEqual(context.processes[0].sent, [CAPTURE])
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, FAST.capture_timeout + 1e-6)
            supervisor.commands.put(None)

        _, _, _, _ = self.run_supervisor(
            [{"initial": [READY]}], emit, on_wait=on_wait,
        )
        self.assertEqual(len(waits), 1)

    def test_successful_camera_probe_prevents_reboot_for_repeated_ai_startup_hangs(self):
        startup_failures = []

        def emit(name, args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)
            elif name == "unavailable" and "AI initialization timed out" in args[0]:
                startup_failures.append(True)
                if len(startup_failures) == 11:
                    commands.put(None)

        supervisor, _, reboots, context = self.run_supervisor(
            [{"initial": [READY], "commands": {"capture": [FAULT]}}]
            + [{"initial": [PROBE_OK]}] * 11,
            emit,
        )
        self.assertEqual(len(context.processes), 12)
        self.assertEqual(supervisor.reconnect_attempts, 0)
        self.assertEqual(reboots, [])

    def test_kill_is_used_if_terminate_does_not_stop_child(self):
        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(None)

        began = time.monotonic()
        _, _, _, context = self.run_supervisor([
            {"initial": [READY], "ignore_terminate": True},
        ], emit)
        process = context.processes[0]
        self.assertEqual(process.terminations, 1)
        self.assertEqual(process.kills, 1)
        self.assertFalse(process.alive)
        self.assertLess(time.monotonic() - began, 0.5)

    def test_unkillable_runtime_child_retries_cleanup_ten_times_then_reboots(self):
        def emit(name, _args, commands, _context):
            if name == "ready":
                commands.put(CAPTURE)

        _, events, reboots, context = self.run_supervisor([
            {"initial": [READY], "ignore_terminate": True, "ignore_kill": True},
        ], emit)
        self.assertEqual(len(context.processes), 1)
        self.assertEqual(reboots, [True])
        self.assertEqual(context.processes[0].kills, 10)
        self.assertEqual(context.processes[0].terminations, 10)
        self.assertTrue(any("could not be stopped" in str(args) for _, args in events))

    def test_unkillable_initial_child_never_reboots(self):
        cleanup_failures = []

        def emit(name, args, commands, _context):
            if name == "unavailable" and "could not be stopped" in args[0]:
                cleanup_failures.append(True)
                if len(cleanup_failures) == 3:
                    commands.put(None)

        supervisor, events, reboots, context = self.run_supervisor([
            {"initial": [ABSENT], "ignore_terminate": True, "ignore_kill": True},
        ], emit)
        self.assertEqual(len(context.processes), 1)
        self.assertFalse(supervisor.ever_ready)
        self.assertEqual(reboots, [])
        self.assertTrue(any("application exit may wait" in str(args) for _, args in events))

    def test_stop_is_prompt_during_initial_retry_delay(self):
        def emit(name, args, commands, _context):
            if name == "unavailable" and "camera absent" in args[0]:
                commands.put(None)

        began = time.monotonic()
        _, _, _, context = self.run_supervisor(
            [{"initial": [ABSENT]}], emit,
            limits=replace(FAST, initial_retry_delay=60),
        )
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(len(context.processes), 1)

    def test_real_spawned_hung_child_is_reaped_without_hardware(self):
        commands = WakeableQueue()
        self.addCleanup(commands.close)
        events = []
        reboots = []

        def emit(name, args):
            events.append((name, args))
            if name == "ready":
                commands.put(CAPTURE)
            elif name == "unavailable" and "frame acquisition timed out" in args[0]:
                commands.put(None)

        supervisor = CameraProcessSupervisor(
            commands, emit, None, None, _hanging_capture_child,
            limits=replace(FAST, startup_timeout=5, capture_timeout=0.05,
                           shutdown_timeout=0.5),
            reboot=lambda: reboots.append(True),
        )
        began = time.monotonic()
        supervisor.run()
        self.assertLess(time.monotonic() - began, 5)
        self.assertIsNone(supervisor._process)
        self.assertIsNone(supervisor._connection)
        self.assertEqual(reboots, [])
        self.assertEqual(sum(name == "failed" for name, _ in events), 1)
        self.assertEqual(events[-1], ("stopped", ()))


class RebootGuardTests(unittest.TestCase):
    def test_desktop_never_runs_reboot_command(self):
        with mock.patch("camera_recovery.platform.system", return_value="Windows"), \
                mock.patch("camera_recovery.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "only available"):
                request_pi_reboot()
            run.assert_not_called()

    def test_pi_reboot_has_durable_cooldown_and_reports_permission_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original_read = Path.read_text

            def read_text(path, *args, **kwargs):
                if str(path).replace("\\", "/") == "/proc/device-tree/model":
                    return "Raspberry Pi 5 Model B\x00"
                return original_read(path, *args, **kwargs)

            with mock.patch("camera_recovery.platform.system", return_value="Linux"), \
                    mock.patch("camera_recovery.Path.home", return_value=home), \
                    mock.patch.object(Path, "read_text", read_text), \
                    mock.patch("camera_recovery.subprocess.run") as run:
                run.return_value.returncode = 1
                run.return_value.stderr = "sudo: a password is required"
                with self.assertRaisesRegex(RuntimeError, "password is required"):
                    request_pi_reboot()
                marker = home / ".local/state/tf_inner/camera_reboot.json"
                self.assertIn("requested_at", json.loads(marker.read_text()))
                with self.assertRaisesRegex(RuntimeError, "last 30 minutes"):
                    request_pi_reboot()
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ["sudo", "-n", "systemctl", "reboot"])


if __name__ == "__main__":
    unittest.main()
