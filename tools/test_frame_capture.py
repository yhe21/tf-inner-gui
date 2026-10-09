"""Exercise the standalone timestamp buffer on a Pi, without the TF GUI."""

import argparse
import json
import math
import platform
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frame_capture import (  # noqa: E402
    CameraSettings,
    CaptureConfig,
    FrameUnavailable,
    Picamera2FrameCapture,
    now_sensor_clock_ns,
)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def describe(record, target_ns):
    return {
        "sequence": record.sequence,
        "signal_timestamp_ns": target_ns,
        "sensor_timestamp_ns": record.sensor_timestamp_ns,
        "age_ms": (target_ns - record.sensor_timestamp_ns) / 1_000_000,
        "shape": list(record.image.shape),
        "bytes": record.image.nbytes,
        "exposure_time_us": record.metadata.get("ExposureTime"),
        "analogue_gain": record.metadata.get("AnalogueGain"),
        "colour_gains": record.metadata.get("ColourGains"),
    }


def save_record(record, directory):
    import numpy as np

    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"frame_{record.sequence}_{record.sensor_timestamp_ns}"
    image_path = stem.with_suffix(".npy")
    np.save(image_path, record.image, allow_pickle=False)
    stem.with_suffix(".json").write_text(
        json.dumps(dict(record.metadata), ensure_ascii=False, indent=2, default=str)
        + "\n", encoding="utf-8"
    )
    return str(image_path)


def self_test(camera, args):
    """Check actual captured history, including wraparound and live signals."""
    required = args.capacity + 20
    deadline = time.monotonic() + args.fill_timeout
    last_progress = 0.0
    while True:
        stats = camera.stats()
        if stats.get("error"):
            raise RuntimeError(str(stats["error"]))
        if stats["total_appended"] >= required:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Buffer did not receive {required} frames: {stats}")
        if time.monotonic() - last_progress >= 5:
            emit({"event": "filling", "count": stats["count"],
                  "total_appended": stats["total_appended"], "required": required})
            last_progress = time.monotonic()
        time.sleep(0.05)

    records = camera.snapshot()
    timestamps = [record.sensor_timestamp_ns for record in records]
    exposures = [record.metadata.get("ExposureTime") for record in records]
    gains = [record.metadata.get("AnalogueGain") for record in records]
    colour_gains = [record.metadata.get("ColourGains") for record in records]
    cached_bytes = sum(record.image.nbytes for record in records)
    del records  # Do not retain another full history while acquisition advances.
    checks = {}

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise AssertionError(f"Hardware check failed: {name}")

    check("full_capacity", len(timestamps) == args.capacity)
    check("strictly_increasing_timestamps", all(
        earlier < later for earlier, later in zip(timestamps, timestamps[1:])
    ))
    check("wraparound_occurred", camera.stats()["evicted"] >= 20)
    midpoint = len(timestamps) // 2
    target = timestamps[midpoint]
    selected = camera.get_before(target, timeout=args.query_timeout)
    check("equal_timestamp_excluded", selected.sensor_timestamp_ns == timestamps[midpoint - 1])
    selected = camera.get_before(target + 1, timeout=args.query_timeout)
    check("closest_strict_predecessor", selected.sensor_timestamp_ns == target)
    try:
        camera.get_before(timestamps[0], timeout=args.query_timeout)
    except FrameUnavailable:
        checks["expired_history_rejected"] = True
    else:
        check("expired_history_rejected", False)
    try:
        camera.get_before(target + 1, timeout=args.query_timeout, max_age_ns=0)
    except FrameUnavailable:
        checks["maximum_age_enforced"] = True
    else:
        check("maximum_age_enforced", False)

    samples = []
    for _ in range(args.signals):
        target = now_sensor_clock_ns()
        before = time.monotonic()
        selected = camera.get_before(target, timeout=args.query_timeout)
        result = describe(selected, target)
        result["lookup_wait_ms"] = (time.monotonic() - before) * 1000
        check("live_signal_strict_predecessor", selected.sensor_timestamp_ns < target)
        # Acquisition may still advance; use a fresh history to verify that no
        # later eligible frame exists, without requiring an exact frame period.
        eligible = [r.sensor_timestamp_ns for r in camera.snapshot()
                    if r.sensor_timestamp_ns < target]
        check("live_signal_closest_available", bool(eligible) and max(eligible) == selected.sensor_timestamp_ns)
        samples.append(result)
        emit({"event": "signal_match", **result})
        time.sleep(0.02)

    intervals_ms = [(b - a) / 1_000_000 for a, b in zip(timestamps, timestamps[1:])]
    median_ms = statistics.median(intervals_ms)
    check("exposure_metadata_present", all(isinstance(x, (float, int)) for x in exposures))
    check("gain_metadata_present", all(isinstance(x, (float, int)) for x in gains))
    expected = camera.settings
    # Sensor line timing and analogue gain steps quantise the requested values.
    # These tolerances still reject clipping a 66 ms exposure to 33 ms, or
    # automatic controls drifting materially during the retained history.
    exposure_tolerance = max(100, expected.exposure_time_us * 0.01)
    check("exposure_matches_setting", all(
        abs(x - expected.exposure_time_us) <= exposure_tolerance for x in exposures
    ))
    check("exposure_stable", max(exposures) - min(exposures) <= exposure_tolerance)
    check("analogue_gain_matches_setting", all(
        math.isclose(x, expected.analogue_gain, rel_tol=0.05, abs_tol=0.01) for x in gains
    ))
    check("analogue_gain_stable", max(gains) - min(gains) <= max(0.01, expected.analogue_gain * 0.01))
    check("colour_gains_match_setting", all(
        isinstance(values, (list, tuple)) and len(values) == 2 and all(
            math.isclose(actual, wanted, rel_tol=0.01, abs_tol=0.02)
            for actual, wanted in zip(values, expected.colour_gains)
        ) for values in colour_gains
    ))
    observed_fps = (len(timestamps) - 1) * 1_000_000_000 / (timestamps[-1] - timestamps[0])
    if args.fps is not None:
        check("requested_fps_observed", math.isclose(observed_fps, args.fps, rel_tol=0.05))
    if args.save_dir:
        saved_image = save_record(selected, args.save_dir)
    else:
        saved_image = None
    try:
        import resource
        peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        peak_rss_kib = None
    return {
        "status": "passed", "hardware_test": True,
        "host": platform.node(), "checks": checks,
        "configuration": vars(args), "statistics": camera.stats(),
        "history_span_seconds": (timestamps[-1] - timestamps[0]) / 1_000_000_000,
        "observed_fps": observed_fps,
        "median_frame_interval_ms": median_ms,
        "maximum_frame_interval_ms": max(intervals_ms),
        "intervals_above_1_5_median": sum(x > median_ms * 1.5 for x in intervals_ms),
        "cached_image_bytes": cached_bytes, "peak_rss_kib": peak_rss_kib,
        "observed_exposure_us_range": [min(exposures), max(exposures)],
        "observed_analogue_gain_range": [min(gains), max(gains)],
        "control_tolerances": {"exposure_us": exposure_tolerance,
                               "analogue_gain_relative": 0.05,
                               "colour_gain_relative": 0.01},
        "matches": samples, "saved_image": saved_image,
    }


def serve_signals(camera, args):
    emit({"event": "ready", "clock": "linux_clock_boottime",
          "now_ns": now_sensor_clock_ns(), "statistics": camera.stats()})
    for line in sys.stdin:
        try:
            signal = json.loads(line)
            if not isinstance(signal, dict):
                raise ValueError("A signal must be a JSON object")
            if signal.get("clock", "linux_clock_boottime") != "linux_clock_boottime":
                raise ValueError("Convert the timestamp to the Pi sensor clock before sending")
            target = signal["timestamp_ns"]
            if isinstance(target, bool) or not isinstance(target, int):
                raise ValueError("timestamp_ns must be an integer number of nanoseconds")
            maximum_age = None if args.max_age_ms is None else int(args.max_age_ms * 1_000_000)
            record = camera.get_before(target, timeout=args.query_timeout,
                                       max_age_ns=maximum_age)
            result = {"event": "frame", **describe(record, target)}
            if args.save_dir:
                result["image_path"] = save_record(record, args.save_dir)
            emit(result)
        except FrameUnavailable as error:
            emit({"event": "unavailable", "reason": error.reason, "message": str(error)})
        except (KeyError, ValueError, TypeError) as error:
            emit({"event": "invalid_signal", "message": str(error)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path,
                        default=Path.home() / ".config/tf_inner/camera_settings.json")
    parser.add_argument("--width", type=int, default=None,
                        help="Explicit output width; omit both dimensions for native sensor resolution")
    parser.add_argument("--height", type=int, default=None,
                        help="Explicit output height; must be supplied together with --width")
    parser.add_argument("--format", dest="pixel_format", choices=("RGB888", "YUV420"), default="RGB888")
    parser.add_argument("--capacity", type=int, default=30)
    parser.add_argument("--fps", type=float, default=None)
    parser.add_argument("--memory-budget-mb", type=float, default=None,
                        help="Optional extra MiB limit; available RAM is always checked")
    parser.add_argument("--camera-num", type=int, default=0)
    parser.add_argument("--query-timeout", type=float, default=2)
    parser.add_argument("--fill-timeout", type=float, default=120)
    parser.add_argument("--max-age-ms", type=float)
    parser.add_argument("--signals", type=int, default=20)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--save-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if (args.width is None) != (args.height is None):
        parser.error("Supply both --width and --height, or omit both for native resolution")
    if args.capacity < 4 or args.signals < 1:
        parser.error("The test runner needs capacity >= 4 and signals >= 1")
    if args.query_timeout <= 0 or args.fill_timeout <= 0:
        parser.error("Timeouts must be positive")
    if args.max_age_ms is not None and args.max_age_ms < 0:
        parser.error("Maximum frame age must be non-negative")
    report = None
    try:
        settings = CameraSettings.from_json(args.settings)
        config = CaptureConfig(
            width=args.width, height=args.height, pixel_format=args.pixel_format,
            capacity=args.capacity, fps=args.fps, memory_budget_mb=args.memory_budget_mb,
            camera_num=args.camera_num,
        )
        with Picamera2FrameCapture(config, settings) as camera:
            if args.self_test:
                report = self_test(camera, args)
            else:
                serve_signals(camera, args)
        # A successful report also requires a clean camera shutdown.
        if report:
            final_stats = camera.stats()
            if final_stats["state"] != "stopped" or final_stats.get("error"):
                raise RuntimeError(f"Capture did not finish cleanly: {final_stats}")
            report["statistics"] = final_stats
            report["checks"]["clean_shutdown"] = True
            emit({"event": "self_test_result", **report})
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        report = {"status": "failed", "hardware_test": True, "host": platform.node(),
                  "error": str(error), "type": type(error).__name__}
        emit({"event": "failure", **report})
        return 1
    finally:
        if args.report and report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)
                                   + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
