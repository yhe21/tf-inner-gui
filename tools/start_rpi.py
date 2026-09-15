"""Check for a safe Git update, then start the Raspberry Pi GUI.

Run with /usr/bin/python3 from the existing desktop autostart entry.
Only Python's standard library is required; no camera or AI imports occur here.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
STARTUP_DELAY_SECONDS = 3
DEFAULT_UPDATE_TIMEOUT_SECONDS = 45


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def run_git(repo: Path, *arguments: str, timeout: float | None = None):
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    command = ["git", "-C", str(repo), *arguments]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=environment, start_new_session=(os.name == "posix"),
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Stop only this Git fetch and its network helpers, never the GUI or
        # another process. The checkout has not been changed during fetch.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, output)


def update_repository(repo: Path, timeout: float) -> bool:
    """Fast-forward a clean main branch; failures keep the existing checkout."""
    try:
        branch = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode != 0 or branch.stdout.strip() != "main":
            log("Update skipped: checkout is not on the main branch.")
            return False

        status = run_git(repo, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0 or status.stdout.strip():
            log("Update skipped: tracked files have local changes or Git status failed.")
            log(status.stdout.strip())
            return False

        log(f"Checking origin/main (network timeout {timeout:g} seconds)...")
        fetched = run_git(repo, "fetch", "--no-tags", "origin", "refs/heads/main",
                          timeout=timeout)
        if fetched.stdout.strip():
            log(fetched.stdout.strip())
        if fetched.returncode != 0:
            log("Fetch failed; starting the existing local version.")
            return False

        current = run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        target = run_git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if current.returncode != 0 or target.returncode != 0:
            log("Update skipped: unable to resolve the current or fetched commit.")
            return False
        current_sha, target_sha = current.stdout.strip(), target.stdout.strip()
        if current_sha == target_sha:
            log(f"Already up to date: {current_sha[:12]}.")
            return True

        ancestor = run_git(repo, "merge-base", "--is-ancestor", current_sha, target_sha)
        if ancestor.returncode != 0:
            log("Update skipped: local commits cannot be fast-forwarded; no reset performed.")
            return False

        # The timeout applies to fetching, NOT checkout: interrupting a checkout
        # could leave GUI files and model weights at different revisions.
        merged = run_git(repo, "merge", "--ff-only", "--no-edit", target_sha)
        if merged.stdout.strip():
            log(merged.stdout.strip())
        if merged.returncode != 0:
            log("Fast-forward failed; attempting to start the existing local checkout.")
            return False
        log(f"Updated {current_sha[:12]} -> {target_sha[:12]} (application and models).")
        return True
    except subprocess.TimeoutExpired:
        log("Update check timed out; starting the existing local version.")
        return False
    except OSError as error:
        log(f"Update unavailable: {error}; starting the existing local version.")
        return False


def launch_gui(repo: Path) -> None:
    application_dir = repo / "tf_gui"
    os.chdir(application_dir)
    log(f"Starting GUI: {sys.executable} main.py --fullscreen")
    # exec preserves the desktop environment and our inherited single-instance
    # lock until the GUI exits. The launcher itself does not remain running.
    os.execv(sys.executable, [sys.executable, "main.py", "--fullscreen"])


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not 0 < seconds <= 600:
        raise argparse.ArgumentTypeError("Use a timeout greater than 0 and at most 600 seconds")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-update", action="store_true",
                        help="Start the installed GUI without checking GitHub.")
    parser.add_argument("--update-timeout", type=positive_seconds,
                        default=DEFAULT_UPDATE_TIMEOUT_SECONDS,
                        help="Maximum fetch time in seconds (default: 45).")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("This startup entry is intended for Raspberry Pi/Linux only.")

    import fcntl

    state_dir = Path.home() / ".local" / "state" / "tf_inner"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "startup.lock").open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("TF GUI launcher is already running; duplicate launch skipped.", flush=True)
            return 0
        os.set_inheritable(lock_file.fileno(), True)

        # Keep the existing log location and per-launch overwrite behavior.
        with (state_dir / "startup.log").open("w", buffering=1) as startup_log:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(startup_log.fileno(), 1)
            os.dup2(startup_log.fileno(), 2)
            log("Desktop startup: waiting 3 seconds before checking for updates.")
            time.sleep(STARTUP_DELAY_SECONDS)
            if args.skip_update:
                log("Update check disabled for this launch.")
            else:
                update_repository(REPOSITORY, args.update_timeout)
            try:
                launch_gui(REPOSITORY)
            except OSError as error:
                log(f"Unable to start GUI: {error}")
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
