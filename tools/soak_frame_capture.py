"""Run continuous capture under load and record Pi temperature for ten minutes."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frame_capture import (  # noqa: E402
    CameraSettings, CaptureConfig, FrameUnavailable, Picamera2FrameCapture,
    now_sensor_clock_ns,
)


THROTTLE_FLAGS = (
    "under_voltage", "frequency_capped", "throttled", "soft_temperature_limit",
)


def probe_system(*, read_text=None, run_command=None):
    """Read optional telemetry; missing data is None with an explicit reason."""
    read_text = read_text or (lambda path: Path(path).read_text(encoding="utf-8"))
    run_command = run_command or subprocess.run
    result = {
        "temperature_c": None, "temperature_error": None,
        "rss_bytes": None, "rss_peak_bytes": None, "rss_error": None,
        "throttled_raw": None, "throttled_value": None,
        "throttled_current": None, "throttled_history": None,
        "throttled_error": None,
    }
    try:
        temperature = float(read_text("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000
        if not math.isfinite(temperature):
            raise ValueError("Temperature is not finite")
        result["temperature_c"] = temperature
    except (OSError, ValueError) as error:
        result["temperature_error"] = f"{type(error).__name__}: {error}"
    try:
        status = read_text("/proc/self/status")
        for key, field in (("VmRSS", "rss_bytes"), ("VmHWM", "rss_peak_bytes")):
            match = re.search(rf"^{key}:\s+(\d+)\s+kB\s*$", status, re.MULTILINE)
            if match:
                result[field] = int(match.group(1)) * 1024
        if result["rss_bytes"] is None:
            raise ValueError("VmRSS is missing from /proc/self/status")
    except (OSError, ValueError) as error:
        result["rss_error"] = f"{type(error).__name__}: {error}"
    try:
        completed = run_command(
            ["vcgencmd", "get_throttled"], timeout=1,
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"vcgencmd exited {completed.returncode}: {completed.stderr.strip()}")
        match = re.fullmatch(r"throttled=(0x[0-9a-fA-F]+)", completed.stdout.strip())
        if not match:
            raise ValueError(f"Unexpected get_throttled output: {completed.stdout.strip()!r}")
        value = int(match.group(1), 16)
        result.update({
            "throttled_raw": match.group(1), "throttled_value": value,
            "throttled_current": {name: bool(value & (1 << bit))
                                  for bit, name in enumerate(THROTTLE_FLAGS)},
            "throttled_history": {name: bool(value & (1 << (bit + 16)))
                                  for bit, name in enumerate(THROTTLE_FLAGS)},
        })
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as error:
        result["throttled_error"] = f"{type(error).__name__}: {error}"
    return result


def _distribution(values):
    return {
        "sample_count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": sum(values) / len(values) if values else None,
    }


def run_soak(camera, args, emit_event, *, probe=probe_system,
             monotonic=time.monotonic, sleep=time.sleep,
             signal_clock=now_sensor_clock_ns):
    """Own camera lifetime and always return a partial or complete scalar report.

    The measured interval starts after filling the RAM ring. Query timeouts are
    recorded and monitoring continues; capture failure or a stalled stream ends
    the run. No image or FrameRecord is kept in samples, queries, or the report.
    """
    report = {
        "status": "failed", "hardware_test": True, "host": platform.node(),
        "configuration": vars(args).copy(),
        "checks": {
            "duration_completed": False, "full_capacity": False,
            "no_query_timeouts": True, "strict_predecessor": True,
            "closest_predecessor": True, "queries_observed": False,
            "capture_healthy": True, "temperature_available": False,
            "clean_shutdown": False,
        },
        "baseline_telemetry": None, "samples": [], "errors": [], "warnings": [],
        "queries": {"attempted": 0, "succeeded": 0, "timeouts": 0, "errors": 0},
        "statistics": None, "summary": {},
    }
    checks = report["checks"]
    ages, waits = [], []
    interrupted = False
    phase = "baseline"
    monitor_start = None
    monitor_end = None
    initial_stats = None
    previous_sample = None
    last_count = None
    last_progress = monotonic()
    capture_attempted = False

    def event(kind, **details):
        emit_event({"event": kind, **details})

    def error_entry(error, error_phase):
        return {"phase": error_phase, "reason": getattr(error, "reason", None),
                "type": type(error).__name__, "message": str(error),
                "elapsed_seconds": None if monitor_start is None else monotonic() - monitor_start}

    def record_error(error, error_phase, kind="failure"):
        entry = error_entry(error, error_phase)
        report["errors"].append(entry)
        try:
            event(kind, **entry)
        except Exception as log_error:
            report["errors"].append(error_entry(log_error, "event_log"))

    def healthy_stats():
        nonlocal last_count, last_progress
        stats = camera.stats()
        if stats.get("error") or stats.get("cleanup_error") or stats.get("state") != "running":
            checks["capture_healthy"] = False
            raise FrameUnavailable("capture_failed", str(
                stats.get("error") or stats.get("cleanup_error") or
                f"Unexpected camera state: {stats.get('state')}"))
        now = monotonic()
        if stats["total_appended"] != last_count:
            last_count, last_progress = stats["total_appended"], now
        elif now - last_progress > args.frame_timeout:
            checks["capture_healthy"] = False
            raise FrameUnavailable("capture_stalled", "No new frame within frame_timeout")
        return stats

    def sample(stats):
        nonlocal previous_sample
        now = monotonic()
        telemetry = probe()
        recent_fps = None
        if previous_sample is not None:
            previous_time, previous_count = previous_sample
            if now > previous_time:
                recent_fps = (stats["total_appended"] - previous_count) / (now - previous_time)
        previous_sample = now, stats["total_appended"]
        row = {
            "elapsed_seconds": now - monitor_start,
            "frame_count": stats["count"], "total_appended": stats["total_appended"],
            "evicted": stats["evicted"], "recent_fps": recent_fps,
            **telemetry,
        }
        report["samples"].append(row)
        event("sample", **row)

    try:
        report["baseline_telemetry"] = probe()
        event("baseline", **report["baseline_telemetry"])
        phase = "startup"
        capture_attempted = True
        camera.start()
        phase = "fill"
        fill_start = monotonic()
        last_filling_event = -math.inf
        while True:
            stats = healthy_stats()
            if stats["count"] == args.capacity:
                checks["full_capacity"] = True
                break
            now = monotonic()
            if now - fill_start >= args.fill_timeout:
                raise FrameUnavailable("fill_timeout", "The ring did not fill before fill_timeout")
            if now - last_filling_event >= args.sample_interval:
                event("filling", count=stats["count"], capacity=args.capacity,
                      total_appended=stats["total_appended"])
                last_filling_event = now
            sleep(min(0.05, max(0, args.fill_timeout - (now - fill_start))))

        phase = "monitor"
        monitor_start = monotonic()
        initial_stats = stats
        deadline = monitor_start + args.duration
        next_sample = monitor_start
        next_query = monitor_start
        event("monitor_started", duration_seconds=args.duration,
              statistics=initial_stats, clock="CLOCK_BOOTTIME")
        while True:
            stats = healthy_stats()
            now = monotonic()
            if now >= deadline:
                checks["duration_completed"] = True
                monitor_end = now
                sample(stats)
                break
            if now >= next_sample:
                sample(stats)
                now = monotonic()
                next_sample += (math.floor((now - next_sample) / args.sample_interval) + 1) * args.sample_interval
            # Keep the full configured lookup timeout. The final short tail is
            # capture-only, rather than falsely timing out a shortened query.
            if deadline - now < args.query_timeout:
                next_query = deadline
            if now < deadline and now >= next_query:
                target = signal_clock()
                started = monotonic()
                report["queries"]["attempted"] += 1
                try:
                    record = camera.get_before(target, timeout=args.query_timeout)
                    selected_timestamp = record.sensor_timestamp_ns
                    sequence = record.sequence
                    del record
                    waited = (monotonic() - started) * 1000
                    age = (target - selected_timestamp) / 1_000_000
                    if selected_timestamp >= target:
                        checks["strict_predecessor"] = False
                        raise AssertionError("Selected frame is not strictly earlier than the signal")
                    # Extract only integers and promptly release the snapshot.
                    eligible = [r.sensor_timestamp_ns for r in camera.snapshot()
                                if r.sensor_timestamp_ns < target]
                    if not eligible or max(eligible) != selected_timestamp:
                        checks["closest_predecessor"] = False
                        raise AssertionError("Selected frame is not the closest retained predecessor")
                    ages.append(age)
                    waits.append(waited)
                    report["queries"]["succeeded"] += 1
                except FrameUnavailable as error:
                    if error.reason != "timeout":
                        report["queries"]["errors"] += 1
                        raise
                    checks["no_query_timeouts"] = False
                    report["queries"]["timeouts"] += 1
                    event("query_timeout", elapsed_seconds=monotonic() - monitor_start,
                          signal_timestamp_ns=target,
                          lookup_wait_ms=(monotonic() - started) * 1000,
                          reason=error.reason, message=str(error))
                except Exception:
                    report["queries"]["errors"] += 1
                    raise
                else:
                    event("query_match", elapsed_seconds=monotonic() - monitor_start,
                          sequence=sequence, signal_timestamp_ns=target,
                          sensor_timestamp_ns=selected_timestamp,
                          age_ms=age, lookup_wait_ms=waited)
                now = monotonic()
                next_query += (math.floor((now - next_query) / args.query_interval) + 1) * args.query_interval
            sleep(max(0, min(0.1, deadline - monotonic(),
                             next_sample - monotonic(), next_query - monotonic())))
    except KeyboardInterrupt:
        interrupted = True
        record_error(KeyboardInterrupt("Test interrupted by user"), phase, "interrupted")
    except Exception as error:
        record_error(error, phase)
    finally:
        if monitor_start is not None and monitor_end is None:
            monitor_end = monotonic()
        if capture_attempted:
            try:
                camera.stop(timeout=args.stop_timeout)
            except KeyboardInterrupt:
                interrupted = True
                record_error(KeyboardInterrupt("Interrupted during cleanup"), "cleanup", "cleanup_error")
            except Exception as error:
                record_error(error, "cleanup", "cleanup_error")
            try:
                report["statistics"] = camera.stats()
                final = report["statistics"]
                checks["clean_shutdown"] = (
                    final.get("state") == "stopped" and not final.get("cleanup_error") and
                    not any(error["phase"] == "cleanup" for error in report["errors"]))
                if final.get("error") or final.get("cleanup_error") or final.get("state") == "failed":
                    checks["capture_healthy"] = False
                    record_error(FrameUnavailable("capture_failed", str(
                        final.get("error") or final.get("cleanup_error") or final.get("state"))),
                        "final_capture_status")
            except Exception as error:
                checks["capture_healthy"] = False
                record_error(error, "final_statistics")

    samples = report["samples"]
    temperatures = [row["temperature_c"] for row in samples if row.get("temperature_c") is not None]
    checks["temperature_available"] = bool(temperatures)
    checks["queries_observed"] = report["queries"]["succeeded"] > 0
    baseline = report["baseline_telemetry"] or {}
    telemetry = [baseline, *samples]
    rss_values = [value for row in telemetry for key in ("rss_bytes", "rss_peak_bytes")
                  if (value := row.get(key)) is not None]
    observed_values = [row["throttled_value"] for row in samples
                       if row.get("throttled_value") is not None]
    baseline_bits = baseline.get("throttled_value")
    current_seen = 0
    new_history = None if baseline_bits is None else 0
    for value in observed_values:
        current_seen |= value & 0xF
        if new_history is not None:
            new_history |= (value & 0xF0000) & ~(baseline_bits & 0xF0000)
    missing = {
        "temperature": sum(row.get("temperature_c") is None for row in samples),
        "rss": sum(row.get("rss_bytes") is None for row in samples),
        "throttled": sum(row.get("throttled_value") is None for row in samples),
    }
    if any(missing.values()):
        report["warnings"].append({"reason": "missing_telemetry", "sample_counts": missing})
    if baseline_bits is None:
        report["warnings"].append({"reason": "throttled_baseline_unavailable",
                                   "message": "New historical events cannot be determined"})
    if current_seen or new_history:
        report["warnings"].append({"reason": "power_or_thermal_flags_observed",
                                   "current_bits": hex(current_seen),
                                   "new_history_bits": None if new_history is None else hex(new_history)})
    elapsed = 0 if monitor_start is None else monitor_end - monitor_start
    final = report["statistics"] or {}
    report["summary"] = {
        "requested_duration_seconds": args.duration, "elapsed_seconds": elapsed,
        "temperature_c": _distribution(temperatures), "peak_rss_bytes": max(rss_values) if rss_values else None,
        "baseline_temperature_c": baseline.get("temperature_c"),
        "first_load_temperature_c": temperatures[0] if temperatures else None,
        "last_load_temperature_c": temperatures[-1] if temperatures else None,
        "telemetry_missing_samples": missing,
        "baseline_throttled_raw": baseline.get("throttled_raw"),
        "observed_current_throttled_bits": hex(current_seen) if observed_values else None,
        "new_throttled_history_bits": None if new_history is None or not observed_values else hex(new_history),
        "throttled_history_note": "History bits were compared with the pre-start baseline; existing bits are not new events",
        "monitor_frames_appended": (final.get("total_appended", 0) - initial_stats["total_appended"]
                                    if initial_stats is not None else 0),
    }
    report["queries"]["age_ms"] = _distribution(ages)
    report["queries"]["lookup_wait_ms"] = _distribution(waits)
    report["status"] = ("interrupted" if interrupted else
                        "passed" if all(checks.values()) and not report["errors"] else "failed")
    try:
        event("finished", status=report["status"], checks=checks,
              summary=report["summary"], queries=report["queries"], warnings=report["warnings"])
    except Exception as error:
        report["errors"].append(error_entry(error, "event_log"))
        if not interrupted:
            report["status"] = "failed"
    return report


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite number")
    return number


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return number


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path,
                        default=Path.home() / ".config/tf_inner/camera_settings.json")
    parser.add_argument("--width", type=_positive_int)
    parser.add_argument("--height", type=_positive_int)
    parser.add_argument("--format", dest="pixel_format", choices=("RGB888", "YUV420"), default="RGB888")
    parser.add_argument("--capacity", type=_positive_int, default=30)
    parser.add_argument("--fps", type=_positive)
    parser.add_argument("--memory-budget-mb", type=_positive)
    parser.add_argument("--camera-num", type=int, default=0)
    for name, default in (
        ("duration", 600), ("sample-interval", 5), ("query-interval", 1),
        ("query-timeout", 2), ("frame-timeout", 0.2), ("start-timeout", 10),
        ("fill-timeout", 120), ("stop-timeout", 3),
    ):
        parser.add_argument(f"--{name}", type=_positive, default=default)
    parser.add_argument("--report", type=Path, default=Path.home() / "frame-capture-soak-yras8.json")
    parser.add_argument("--log", type=Path, default=Path.home() / "frame-capture-soak-yras8.jsonl")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.width is None) != (args.height is None):
        parser.error("Supply both --width and --height, or omit both for native sensor resolution")
    if args.camera_num < 0:
        parser.error("--camera-num must be non-negative")
    if args.report.expanduser().resolve() == args.log.expanduser().resolve():
        parser.error("--report and --log must be different paths")
    args.report, args.log = args.report.expanduser(), args.log.expanduser()
    report = None
    try:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("w", encoding="utf-8") as log:
            def emit_event(value):
                line = json.dumps(value, ensure_ascii=False, default=str)
                log.write(line + "\n")
                log.flush()
                # Matches remain in JSONL; keep the terminal readable at 5 s intervals.
                if value.get("event") == "sample":
                    temp = value.get("temperature_c")
                    rss = value.get("rss_bytes")
                    fps = value.get("recent_fps")
                    print(
                        f"{value['elapsed_seconds']:7.1f}s | "
                        f"temperature={'unavailable' if temp is None else f'{temp:.1f} C'} | "
                        f"fps={'n/a' if fps is None else f'{fps:.2f}'} | "
                        f"RSS={'unavailable' if rss is None else f'{rss / 1024**2:.1f} MiB'} | "
                        f"frames={value['total_appended']} | "
                        f"throttled={value.get('throttled_raw') or 'unavailable'}",
                        flush=True,
                    )
                elif value.get("event") != "query_match":
                    print(line, flush=True)

            settings = CameraSettings.from_json(args.settings)
            config = CaptureConfig(
                width=args.width, height=args.height, pixel_format=args.pixel_format,
                capacity=args.capacity, fps=args.fps, memory_budget_mb=args.memory_budget_mb,
                camera_num=args.camera_num, frame_timeout=args.frame_timeout,
                start_timeout=args.start_timeout,
            )
            camera = Picamera2FrameCapture(config, settings)
            report = run_soak(camera, args, emit_event)
    except KeyboardInterrupt:
        if report is None:
            report = {"status": "interrupted", "error": "Interrupted before monitoring started"}
        else:
            report["status"] = "interrupted"
    except Exception as error:
        failure = {"error": str(error), "type": type(error).__name__}
        if report is None:
            report = {"status": "failed", **failure}
        else:
            report["errors"].append({"phase": "output", "reason": None,
                                     "type": type(error).__name__, "message": str(error)})
            if report["status"] != "interrupted":
                report["status"] = "failed"
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr, flush=True)
    if report is not None:
        try:
            temporary = args.report.with_name(args.report.name + ".tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                                 encoding="utf-8")
            temporary.replace(args.report)
            print(f"Status: {report['status']} | report: {args.report} | log: {args.log}", flush=True)
        except OSError as error:
            print(f"Could not save final report: {error}; partial events: {args.log}", file=sys.stderr)
            return 130 if report["status"] == "interrupted" else 1
    return {"passed": 0, "failed": 1, "interrupted": 130}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
