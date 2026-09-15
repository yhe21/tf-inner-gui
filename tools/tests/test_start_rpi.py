import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from types import ModuleType


SCRIPT = Path(__file__).resolve().parents[1] / "start_rpi.py"
SPEC = importlib.util.spec_from_file_location("start_rpi", SCRIPT)
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)
OLD = "1" * 40
NEW = "2" * 40


def result(text="", code=0):
    return subprocess.CompletedProcess([], code, text)


class StartupTests(unittest.TestCase):
    def test_copy_ready_autostart_has_one_background_launcher(self):
        template = SCRIPT.parent / "rpi" / "labwc" / "autostart"
        contents = template.read_bytes()
        self.assertTrue(contents.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", contents)
        commands = [line for line in contents.decode("ascii").splitlines()
                    if line.strip() and not line.startswith("#")]
        self.assertEqual(commands, [
            '/usr/bin/python3 "$HOME/tf-inner-gui/tools/start_rpi.py" &',
        ])

    def test_git_preserves_linux_line_endings_for_autostart(self):
        attributes = SCRIPT.parent.parent / ".gitattributes"
        self.assertIn("tools/rpi/labwc/autostart text eol=lf",
                      attributes.read_text(encoding="utf-8").splitlines())

    def run_update(self, responses):
        with mock.patch.object(startup, "run_git", side_effect=responses) as git:
            with redirect_stdout(io.StringIO()) as output:
                success = startup.update_repository(Path("repository"), 45)
        return success, git.call_args_list, output.getvalue()

    def test_clean_main_fast_forwards_the_fetched_commit(self):
        success, calls, output = self.run_update([
            result("main\n"), result(), result(), result(OLD), result(NEW),
            result(), result("Fast-forward"),
        ])
        self.assertTrue(success)
        self.assertEqual(calls[2].args[1:],
                         ("fetch", "--no-tags", "origin", "refs/heads/main"))
        self.assertEqual(calls[2].kwargs, {"timeout": 45})
        self.assertEqual(calls[-1].args[1:], ("merge", "--ff-only", "--no-edit", NEW))
        self.assertEqual(calls[-1].kwargs, {})
        self.assertIn("Updated", output)

    def test_up_to_date_does_not_merge(self):
        success, calls, output = self.run_update([
            result("main"), result(), result(), result(OLD), result(OLD),
        ])
        self.assertTrue(success)
        self.assertEqual(len(calls), 5)
        self.assertIn("Already up to date", output)

    def test_changed_tracked_files_are_not_overwritten(self):
        success, calls, output = self.run_update([result("main"), result(" M tf_gui/main.py")])
        self.assertFalse(success)
        self.assertEqual(len(calls), 2)
        self.assertIn("local changes", output)

    def test_non_main_or_detached_checkout_is_left_alone(self):
        for branch in (result("custom"), result(code=1)):
            with self.subTest(branch=branch):
                success, calls, _ = self.run_update([branch])
                self.assertFalse(success)
                self.assertEqual(len(calls), 1)

    def test_status_failure_skips_update(self):
        success, calls, _ = self.run_update([result("main"), result("index error", 1)])
        self.assertFalse(success)
        self.assertEqual(len(calls), 2)

    def test_failed_fetch_does_not_merge_stale_fetch_head(self):
        success, calls, output = self.run_update([
            result("main"), result(), result("Could not resolve host", 128),
        ])
        self.assertFalse(success)
        self.assertEqual(len(calls), 3)
        self.assertIn("existing local version", output)

    def test_timeout_and_missing_git_do_not_prevent_fallback(self):
        for error in (subprocess.TimeoutExpired("git fetch", 45), FileNotFoundError("git")):
            with self.subTest(error=error):
                success, calls, output = self.run_update([result("main"), result(), error])
                self.assertFalse(success)
                self.assertEqual(len(calls), 3)
                self.assertIn("existing local version", output)

    def test_local_commits_are_not_reset_or_rebased(self):
        success, calls, output = self.run_update([
            result("main"), result(), result(), result(OLD), result(NEW), result(code=1),
        ])
        self.assertFalse(success)
        self.assertEqual(len(calls), 6)
        self.assertIn("no reset performed", output)

    def test_merge_failure_is_reported_without_retrying_destructively(self):
        success, calls, output = self.run_update([
            result("main"), result(), result(), result(OLD), result(NEW),
            result(), result("Untracked file would be overwritten", 1),
        ])
        self.assertFalse(success)
        self.assertEqual(len(calls), 7)
        self.assertIn("Fast-forward failed", output)

    def test_gui_uses_the_same_python_and_original_fullscreen_command(self):
        repo = Path("repository")
        with mock.patch.object(startup.os, "chdir") as chdir:
            with mock.patch.object(startup.os, "execv") as execute:
                with redirect_stdout(io.StringIO()):
                    startup.launch_gui(repo)
        chdir.assert_called_once_with(repo / "tf_gui")
        execute.assert_called_once_with(
            startup.sys.executable, [startup.sys.executable, "main.py", "--fullscreen"]
        )

    def test_git_is_noninteractive_and_captures_output(self):
        child = mock.Mock(returncode=0)
        child.communicate.return_value = ("main\n", None)
        with mock.patch.object(startup.subprocess, "Popen", return_value=child) as popen:
            completed = startup.run_git(Path("repo"), "fetch", timeout=45)
        self.assertEqual(completed.stdout, "main\n")
        self.assertEqual(popen.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(popen.call_args.kwargs["env"]["GCM_INTERACTIVE"], "Never")
        child.communicate.assert_called_once_with(timeout=45)

    def test_timeout_stops_only_the_git_process_group_on_linux(self):
        child = mock.Mock(pid=4321)
        child.communicate.side_effect = [subprocess.TimeoutExpired("git", 45), ("", None)]
        with mock.patch.object(startup.subprocess, "Popen", return_value=child):
            with mock.patch.object(startup.os, "name", "posix"):
                with mock.patch.object(startup.os, "killpg", create=True) as killpg:
                    with mock.patch.object(startup.signal, "SIGKILL", 9, create=True):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            startup.run_git("repo", "fetch", timeout=45)
        killpg.assert_called_once_with(4321, 9)

    def test_launcher_starts_gui_even_when_update_is_skipped_or_failed(self):
        for flags in ([], ["--skip-update"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                fcntl = ModuleType("fcntl")
                fcntl.LOCK_EX, fcntl.LOCK_NB = 2, 4
                fcntl.flock = mock.Mock()
                with mock.patch.dict(startup.sys.modules, {"fcntl": fcntl}), \
                     mock.patch.object(startup.sys, "platform", "linux"), \
                     mock.patch.object(startup.sys, "argv", [str(SCRIPT), *flags]), \
                     mock.patch.object(startup.Path, "home", return_value=Path(directory)), \
                     mock.patch.object(startup.os, "dup2"), \
                     mock.patch.object(startup.os, "set_inheritable") as inherit, \
                     mock.patch.object(startup.time, "sleep") as delay, \
                     mock.patch.object(startup, "update_repository", return_value=False) as update, \
                     mock.patch.object(startup, "launch_gui") as launch, \
                     redirect_stdout(io.StringIO()):
                    startup.main()
                launch.assert_called_once_with(startup.REPOSITORY)
                delay.assert_called_once_with(3)
                self.assertTrue(inherit.call_args.args[1])
                if flags:
                    update.assert_not_called()
                else:
                    update.assert_called_once_with(startup.REPOSITORY, 45)

    def test_duplicate_launcher_preserves_active_log_and_does_not_update(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / ".local" / "state" / "tf_inner"
            state.mkdir(parents=True)
            logfile = state / "startup.log"
            logfile.write_text("active GUI log", encoding="utf-8")
            fcntl = ModuleType("fcntl")
            fcntl.LOCK_EX, fcntl.LOCK_NB = 2, 4
            fcntl.flock = mock.Mock(side_effect=BlockingIOError)
            with mock.patch.dict(startup.sys.modules, {"fcntl": fcntl}), \
                 mock.patch.object(startup.sys, "platform", "linux"), \
                 mock.patch.object(startup.sys, "argv", [str(SCRIPT)]), \
                 mock.patch.object(startup.Path, "home", return_value=Path(directory)), \
                 mock.patch.object(startup, "update_repository") as update, \
                 mock.patch.object(startup, "launch_gui") as launch, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(startup.main(), 0)
            update.assert_not_called()
            launch.assert_not_called()
            self.assertEqual(logfile.read_text(encoding="utf-8"), "active GUI log")

    @unittest.skipUnless(shutil.which("git"), "Git is required for the local integration test")
    def test_real_local_git_update_then_dirty_file_protection(self):
        # All repositories are temporary, and origin is a local directory: this
        # exercises real fetch/merge commands without GitHub or production files.
        with tempfile.TemporaryDirectory(prefix="tf-startup-test-") as directory:
            root = Path(directory)
            origin, checkout = root / "origin", root / "checkout"

            def git(*arguments):
                return subprocess.run(
                    ["git", *map(str, arguments)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                ).stdout.strip()

            git("init", "-b", "main", origin)
            git("-C", origin, "config", "user.name", "Startup Test")
            git("-C", origin, "config", "user.email", "startup-test@example.invalid")
            git("-C", origin, "config", "core.hooksPath", str(root / "no-hooks"))
            (origin / "app.txt").write_text("version 1\n", encoding="utf-8")
            git("-C", origin, "add", "app.txt")
            git("-C", origin, "commit", "-m", "version 1")
            git("clone", origin, checkout)
            git("-C", checkout, "config", "core.hooksPath", str(root / "no-hooks"))
            (origin / "app.txt").write_text("version 2\n", encoding="utf-8")
            git("-C", origin, "commit", "-am", "version 2")
            with redirect_stdout(io.StringIO()):
                self.assertTrue(startup.update_repository(checkout, 10))
            self.assertEqual(git("-C", checkout, "rev-parse", "HEAD"),
                             git("-C", origin, "rev-parse", "HEAD"))
            self.assertEqual((checkout / "app.txt").read_text(), "version 2\n")
            (checkout / "app.txt").write_text("local changes\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertFalse(startup.update_repository(checkout, 10))
            self.assertEqual((checkout / "app.txt").read_text(), "local changes\n")


if __name__ == "__main__":
    unittest.main()
