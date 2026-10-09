"""Exercise independent INNER/GLUE duty cycling on a Pi, without inference."""
import argparse
import json
import math
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frame_capture import (  # noqa: E402
    CameraSettings, CaptureConfig, KeepAliveFrameCapture, OnDemandFrameCapture,
    TriggerConfig, TriggeredFrameCapture, now_sensor_clock_ns,
)
from tools.soak_frame_capture import probe_system  # noqa: E402
from tools.test_frame_capture import describe  # noqa: E402


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite number")
    return number


def check_settings(frame, settings):
    """Check each delivered single/glue frame, rather than assume startup settled."""
    metadata = frame.metadata
    exposure = metadata.get("ExposureTime")
    gain = metadata.get("AnalogueGain")
    colours = metadata.get("ColourGains")
    if (exposure is None or not math.isfinite(exposure) or
            abs(exposure - settings.exposure_time_us) > max(100, settings.exposure_time_us * 0.01)):
        raise AssertionError(f"Frame exposure does not match fixed setting: {exposure}")
    if gain is None or not math.isclose(gain, settings.analogue_gain, rel_tol=0.05, abs_tol=0.01):
        raise AssertionError(f"Frame analogue gain does not match fixed setting: {gain}")
    if (not isinstance(colours, (tuple, list)) or len(colours) != 2 or not all(
            math.isclose(actual, wanted, rel_tol=0.01, abs_tol=0.02)
            for actual, wanted in zip(colours, settings.colour_gains))):
        raise AssertionError(f"Frame colour gains do not match fixed setting: {colours}")


def check_keep_alive(state, *, closed=False):
    """A passing warm test must prove hardware stayed started across pauses."""
    expected = {"camera_started": not closed, "camera_open_count": 1,
                "camera_start_count": 1, "camera_stop_count": int(closed),
                "camera_close_count": int(closed)}
    actual = {key: state.get(key) for key in expected}
    if actual != expected:
        raise AssertionError(f"Keep-alive camera lifecycle mismatch: {actual}; expected {expected}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path.home() / ".config/tf_inner/camera_settings.json")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--duration", type=positive, help="Repeat complete cycles for at least this many seconds")
    parser.add_argument("--start-delay", type=positive, default=3.0)
    parser.add_argument("--max-duration", type=positive, default=6.0)
    parser.add_argument("--glue-delay", type=positive, default=5.0,
                        help="Send GLUE this many seconds after INNER image is returned")
    parser.add_argument("--idle-between-cycles", type=positive, default=1.0)
    parser.add_argument("--frame-timeout", type=positive, default=0.2)
    parser.add_argument("--query-timeout", type=positive, default=2.0)
    parser.add_argument("--capacity", type=int, default=30)
    parser.add_argument("--camera-mode", choices=("keep-alive", "close-between"), default="keep-alive",
                        help="Keep hardware started while idle (default), or close after each capture window")
    parser.add_argument("--single-only", action="store_true", help="Disable delayed buffering; both events capture one new frame")
    parser.add_argument("--skip-glue", action="store_true", help="Do not send GLUE; verify the automatic duration stop")
    parser.add_argument("--report", type=Path, default=Path.home() / "frame-capture-triggered.json")
    parser.add_argument("--log", type=Path, default=Path.home() / "frame-capture-triggered.jsonl")
    args = parser.parse_args(argv)
    if args.cycles <= 0 or args.capacity <= 0:
        parser.error("cycles and capacity must be positive")
    if args.single_only and args.skip_glue:
        parser.error("--skip-glue requires delayed buffering")
    if args.log.expanduser().resolve() == args.report.expanduser().resolve():
        parser.error("report and log must have different paths")
    args.log, args.report = args.log.expanduser(), args.report.expanduser()
    report = {"status": "failed", "hardware_test": True, "host": platform.node(),
              "configuration": vars(args), "cycles": [], "samples": [], "errors": [], "warnings": []}
    workflow = None
    started = time.monotonic()
    try:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("w", encoding="utf-8") as log:
            next_sample = 0.0
            def emit(event, **data):
                line = json.dumps({"event": event, "elapsed_seconds": time.monotonic() - started,
                                   **data}, ensure_ascii=False, default=str)
                log.write(line + "\n")
                log.flush()
                print(line, flush=True)

            def sample():
                nonlocal next_sample
                row = {"elapsed_seconds": time.monotonic() - started, **probe_system()}
                report["samples"].append(row)
                emit("temperature", **{k: v for k, v in row.items() if k != "elapsed_seconds"})
                next_sample = time.monotonic() + 1.0

            def wait_until(deadline):
                while time.monotonic() < deadline:
                    state = workflow.stats()
                    if (state["error"] or state["session"].get("error") or
                            state["session"].get("cleanup_error")):
                        raise RuntimeError(f"Capture failed during wait: {state}")
                    if time.monotonic() >= next_sample:
                        sample()
                    time.sleep(max(0, min(0.05, deadline - time.monotonic())))

            try:
                settings = CameraSettings.from_json(args.settings)
                session_type = (KeepAliveFrameCapture if args.camera_mode == "keep-alive"
                                else OnDemandFrameCapture)
                session = session_type(
                    CaptureConfig(capacity=args.capacity, frame_timeout=args.frame_timeout), settings)
                workflow = TriggeredFrameCapture(session, TriggerConfig(
                    enabled=not args.single_only, start_delay=args.start_delay,
                    max_duration=args.max_duration, query_timeout=args.query_timeout))
                sample()
                cycle_index = 0
                while cycle_index == 0 or (time.monotonic() - started < args.duration if args.duration is not None
                                          else cycle_index < args.cycles):
                    cycle_index += 1
                    cycle = {"cycle": cycle_index}
                    report["cycles"].append(cycle)
                    signal = now_sensor_clock_ns()
                    before = time.monotonic()
                    inner = workflow.on_inner()
                    inner_finished = time.monotonic()
                    check_settings(inner, settings)
                    cycle["inner"] = {**describe(inner, signal),
                                      "capture_wait_ms": (inner_finished - before) * 1000}
                    del inner
                    emit("inner_frame", **cycle["inner"])
                    cycle["after_inner"] = workflow.stats()
                    if args.camera_mode == "keep-alive":
                        check_keep_alive(cycle["after_inner"]["session"])
                    if args.skip_glue:
                        # Include bounded startup and cleanup allowance, but
                        # only pass when the module stopped itself normally.
                        deadline = inner_finished + args.start_delay + 10 + args.max_duration + 3 + 1
                        while time.monotonic() < deadline:
                            wait_until(min(deadline, time.monotonic() + 0.5))
                            state = session.stats()
                            if state["state"] == "idle" and state.get("stop_reason") == "duration_limit":
                                break
                        else:
                            raise TimeoutError("Continuous capture did not automatically stop")
                        if session.stats()["state"] != "idle":
                            raise AssertionError("Automatic stop did not finish pausing application capture")
                        cycle["automatic_stop"] = True
                    else:
                        wait_until(inner_finished + args.glue_delay)
                        signal = now_sensor_clock_ns()
                        before = time.monotonic()
                        glue = workflow.on_glue(signal)
                        check_settings(glue, settings)
                        cycle["glue"] = {**describe(glue, signal),
                                         "lookup_wait_ms": (time.monotonic() - before) * 1000}
                        if not args.single_only and glue.sensor_timestamp_ns >= signal:
                            raise AssertionError("GLUE frame must precede its signal timestamp")
                        del glue
                        emit("glue_frame", **cycle["glue"])
                    cycle["after_cycle"] = workflow.stats()
                    stopped = cycle["after_cycle"]["session"]
                    if (cycle["after_cycle"].get("error") or stopped.get("error") or
                            stopped.get("cleanup_error") or stopped["state"] != "idle"):
                        raise AssertionError(f"Camera did not finish this cycle cleanly: {cycle['after_cycle']}")
                    if args.camera_mode == "keep-alive":
                        check_keep_alive(stopped)
                    cycle["passed"] = True
                    emit("cycle_complete", cycle=cycle_index, state=cycle["after_cycle"])
                    sample()
                    if args.duration is not None or cycle_index < args.cycles:
                        wait_until(time.monotonic() + args.idle_between_cycles)
                        if args.camera_mode == "keep-alive":
                            after_idle = session.stats()
                            check_keep_alive(after_idle)
                            if after_idle.get("copied_frames_total") != stopped.get("copied_frames_total"):
                                raise AssertionError("Images were copied during the idle interval")
                report["status"] = "passed"
            except KeyboardInterrupt:
                report["status"] = "interrupted"
                report["errors"].append("Interrupted by user")
            except Exception as error:
                report["errors"].append(f"{type(error).__name__}: {error}")
            finally:
                if workflow is not None:
                    try:
                        workflow.close()
                    except Exception as error:
                        report["errors"].append(f"Cleanup failed: {error}")
                        if report["status"] != "interrupted":
                            report["status"] = "failed"
                    report["final_statistics"] = workflow.stats()
                    final = report["final_statistics"]
                    stopped = final["session"]
                    if (final.get("error") or stopped.get("error") or stopped.get("cleanup_error") or
                            stopped["state"] != "closed"):
                        report["errors"].append(f"Final camera status is not clean: {final}")
                        if report["status"] != "interrupted":
                            report["status"] = "failed"
                    if args.camera_mode == "keep-alive" and report["status"] == "passed":
                        try:
                            check_keep_alive(stopped, closed=True)
                        except AssertionError as error:
                            report["errors"].append(str(error))
                            report["status"] = "failed"
                temps = [row["temperature_c"] for row in report["samples"] if row["temperature_c"] is not None]
                if not temps:
                    report["warnings"].append("Processor temperature unavailable; thermal benefit is unverified")
                report["summary"] = {
                    "camera_mode": args.camera_mode,
                    "elapsed_seconds": time.monotonic() - started,
                    "cycles_completed": sum(row.get("passed", False) for row in report["cycles"]),
                    "temperature_min_c": min(temps) if temps else None,
                    "temperature_max_c": max(temps) if temps else None,
                    "temperature_first_c": temps[0] if temps else None,
                    "temperature_last_c": temps[-1] if temps else None,
                }
                for phase, field in (("inner", "capture_wait_ms"), ("glue", "lookup_wait_ms")):
                    values = [row[phase][field] for row in report["cycles"] if phase in row]
                    report["summary"][f"{phase}_wait_mean_ms"] = sum(values) / len(values) if values else None
                    report["summary"][f"{phase}_wait_max_ms"] = max(values) if values else None
                if args.camera_mode == "keep-alive" and "final_statistics" in report:
                    final_session = report["final_statistics"]["session"]
                    report["summary"]["camera_lifecycle"] = {
                        key: final_session.get(key) for key in (
                            "camera_open_count", "camera_start_count", "camera_stop_count",
                            "camera_close_count", "copied_frames_total")}
                emit("finished", status=report["status"], summary=report["summary"],
                     errors=report["errors"], warnings=report["warnings"])
    except Exception as error:
        report["status"] = "failed"
        report["errors"].append(f"Output failure: {error}")
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return {"passed": 0, "failed": 1, "interrupted": 130}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
