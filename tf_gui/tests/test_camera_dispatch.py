"""Real wait handles and spawned children, without cameras, models or robots."""

from dataclasses import replace
from multiprocessing.connection import wait
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_recovery import (  # noqa: E402
    CameraProcessSupervisor,
    RecoveryLimits,
    WakeableQueue,
)


CAPTURE = ("capture", Path("image.jpg"), True, "GLUE", False)


def _responsive_child(connection, _config):
    connection.send(("startup_probe_succeeded", ()))
    connection.send(("ready", (4056, 3040, True)))
    while True:
        command = connection.recv()
        if command is None:
            return
        connection.send(("test_command_received", (command[0],)))
        if command[0] == "capture":
            connection.send(("capture_started", (str(command[1]), command[2])))
            connection.send(("frame_acquired", (1,)))
            connection.send(("succeeded", (str(command[1]), command[2])))


def _exiting_idle_child(connection, _config):
    connection.send(("startup_probe_succeeded", ()))
    connection.send(("ready", (4056, 3040, True)))
    # An exit, not a camera operation or periodic health check, wakes the parent.
    time.sleep(0.1)


class WakeableQueueTests(unittest.TestCase):
    def setUp(self):
        self.commands = WakeableQueue()
        self.addCleanup(self.commands.close)

    def test_notification_stays_ready_until_last_command_is_taken(self):
        self.assertEqual(wait([self.commands.reader], 0), [])
        self.commands.put("first")
        self.commands.put("second")
        self.assertEqual(wait([self.commands.reader], 0), [self.commands.reader])
        self.assertEqual(self.commands.get_nowait(), "first")
        self.assertEqual(wait([self.commands.reader], 0), [self.commands.reader])
        self.assertEqual(self.commands.get_nowait(), "second")
        self.assertEqual(wait([self.commands.reader], 0), [])
        self.commands.put(None)
        self.assertEqual(wait([self.commands.reader], 0), [self.commands.reader])
        self.assertIsNone(self.commands.get_nowait())
        self.assertEqual(wait([self.commands.reader], 0), [])

    def test_put_wakes_an_indefinite_os_wait(self):
        entered = threading.Event()
        received = []

        def block_until_command():
            entered.set()
            received.extend(wait([self.commands.reader], None))

        waiter = threading.Thread(target=block_until_command, daemon=True)
        waiter.start()
        self.assertTrue(entered.wait(1))
        self.commands.put(CAPTURE)
        waiter.join(1)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(received, [self.commands.reader])
        self.assertEqual(self.commands.get_nowait(), CAPTURE)


class SpawnedDispatchTests(unittest.TestCase):
    def start_supervisor(self, target):
        commands = WakeableQueue()
        events = []
        idle_wait = threading.Event()
        completed = threading.Event()
        failure = threading.Event()
        reboots = []
        waits = []

        def emit(name, args):
            events.append((name, args))
            if name == "succeeded":
                completed.set()
            elif name == "unavailable" and args[0].startswith("Camera unavailable:"):
                failure.set()
                commands.put(None)

        supervisor = CameraProcessSupervisor(
            commands, emit, None, None, target,
            limits=replace(RecoveryLimits(), startup_timeout=5,
                           capture_timeout=0.5, shutdown_timeout=0.5),
            reboot=lambda: reboots.append(True),
        )
        original_wait = supervisor._wait_for_activity

        def observed_wait(timeout, child=True):
            waits.append(timeout)
            if supervisor._child_ready and supervisor._active is None:
                idle_wait.set()
            return original_wait(timeout, child=child)

        supervisor._wait_for_activity = observed_wait
        thread = threading.Thread(target=supervisor.run, daemon=True)
        thread.start()

        def cleanup():
            if thread.is_alive():
                commands.put(None)
                thread.join(2)
            if not thread.is_alive():
                commands.close()

        self.addCleanup(cleanup)
        return (supervisor, commands, thread, events, idle_wait, completed,
                failure, reboots, waits)

    def test_idle_has_no_periodic_wake_or_capture_then_command_and_stop_wake_it(self):
        (supervisor, commands, thread, events, idle_wait, completed,
         _failure, reboots, waits) = self.start_supervisor(_responsive_child)
        self.assertTrue(idle_wait.wait(5), events)
        self.assertIsNone(waits[-1])
        before = len(waits)
        # While idle the wait is indefinite, not an interval-based health tick.
        self.assertFalse(completed.wait(0.15))
        self.assertEqual(len(waits), before)
        self.assertFalse(any(name == "test_command_received" for name, _ in events))
        began = time.monotonic()
        commands.put(CAPTURE)
        self.assertTrue(completed.wait(1), events)
        self.assertLess(time.monotonic() - began, 0.5)
        commands.put(None)
        thread.join(2)
        self.assertFalse(thread.is_alive(), events)
        self.assertEqual([args for name, args in events
                          if name == "test_command_received"], [("capture",)])
        self.assertEqual(events[-1], ("stopped", ()))
        self.assertIsNone(supervisor._process)
        self.assertEqual(reboots, [])

    def test_idle_child_exit_wakes_indefinite_wait_without_health_capture(self):
        (supervisor, _commands, thread, events, idle_wait, _completed,
         failure, reboots, waits) = self.start_supervisor(_exiting_idle_child)
        self.assertTrue(idle_wait.wait(5), events)
        self.assertIsNone(waits[-1])
        self.assertTrue(failure.wait(1), events)
        thread.join(2)
        self.assertFalse(thread.is_alive(), events)
        self.assertIsNone(supervisor._process)
        self.assertFalse(any(name == "test_command_received" for name, _ in events))
        self.assertEqual(reboots, [])


if __name__ == "__main__":
    unittest.main()
