"""Small, fixed-endpoint encoder tests. No external metrology is implied."""

import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path


def summarize(rows):
    groups = {}
    for row in rows:
        if row["status"] == "OK":
            groups.setdefault(str(row["target_inc"]), []).append(row["settled_inc"])
    return {
        target: {
            "n": len(values),
            "mean_error_inc": statistics.mean(values) - int(target),
            "sample_sd_inc": statistics.stdev(values) if len(values) > 1 else None,
            "peak_to_peak_inc": max(values) - min(values),
        }
        for target, values in groups.items()
    }


def run_repeatability(app, config):
    cycles = int(config["cycles"])
    span = int(config["span_inc"])
    speed = int(config["speed_inc_s"])
    if not 2 <= cycles <= 20 or not 2 <= span <= 100 or not 1 <= speed <= 20:
        raise ValueError("Use 2..20 cycles, 2..100 counts and 1..20 counts/s.")
    folder = Path(config["output_dir"]) / datetime.now().strftime("repeatability_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    report = {
        "started_local": datetime.now().astimezone().isoformat(),
        "measurement": "Internal encoder repeatability; NOT external absolute accuracy",
        "encoder_setting": config["encoder"],
        "encoder_label_verified": bool(config["encoder_verified"]),
        "um_per_count_configured": app._inc_to_um(1),
        "requested_cycles": cycles,
        "span_inc": span,
        "speed_inc_s": speed,
        "status": "INCOMPLETE",
    }
    node = None
    try:
        node = app._require_node()
        origin = app._read_actual_position_inc(node, fresh=True)
        report["origin_inc"] = origin
        report["node_id"] = node.id
        report["serial"] = hex(node.sdo[0x1018][4].raw)
    except Exception as exc:
        report["status"] = "STOPPED"
        report["reason"] = f"Initial device read failed: {exc}"
        if node is not None and not app.__dict__.get("stop_pending", False):
            try:
                app._disable_drive_worker()
                report["final_statusword"] = hex(app._read_statusword(node, fresh=True))
                report["final_error_register"] = int(node.sdo[0x1001].raw)
                report["final_position_inc"] = app._read_actual_position_inc(node, fresh=True)
            except Exception as stop_exc:
                report["shutdown_error"] = str(stop_exc)
        report["completed_steps"] = 0
        report["endpoints"] = {}
        report["output_dir"] = str(folder.resolve())
        (folder / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        app._post("log", f"Repeatability STOPPED: {folder}")
        return report

    profile = dict(config["profile"])
    local_min = app._inc_to_um(origin - span - 10)
    local_max = app._inc_to_um(origin + span + 10)
    base_limits = dict(profile.get("limits", {}))
    if base_limits.get("enabled"):
        local_min = max(local_min, float(base_limits["min"]))
        local_max = min(local_max, float(base_limits["max"]))
    profile["limits"] = {"enabled": True, "min": local_min, "max": local_max}
    # Count-based profiles remain interpretable before the encoder label is confirmed.
    for name, value in (("acceleration", 20), ("max_acceleration", 100),
                        ("quick_stop_decel", 100), ("following_error_window", 20),
                        ("position_window", 2)):
        profile[name + "_inc"] = value
        profile[name] = app._inc_to_um(value)
    if "max_velocity_inc" not in profile:
        profile["max_velocity_inc"] = 20
        profile["max_velocity"] = app._inc_to_um(20)
    profile["following_error_timeout"] = 1000
    profile["position_window_time"] = 200
    app.runtime_position_window_inc = 2
    fields = ["step", "cycle", "leg", "start_inc", "target_inc", "end_inc", "settled_inc",
              "error_inc", "duration", "status"]
    try:
        with (folder / "endpoints.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for cycle in range(1, cycles + 1):
                for leg, target in (("out", origin + span), ("return", origin)):
                    if app.test_stop_requested.is_set():
                        raise RuntimeError("Stopped by user")
                    current = app._read_actual_position_inc(node, fresh=True)
                    if abs(current - origin) > span + 10:
                        raise RuntimeError("Position outside local test envelope")
                    result = app._run_test_move(
                        node, config["axis"], len(rows) + 1, app._inc_to_um(speed),
                        app._inc_to_um(target - current), profile, 15 + 2 * span / speed,
                        absolute_target_inc=target,
                    )
                    result.update(cycle=cycle, leg=leg)
                    result["settled_inc"] = result["end_inc"]
                    if result["status"] == "OK":
                        if app.test_stop_requested.wait(0.5):
                            result["status"] = "Stopped"
                        else:
                            try:
                                result["settled_inc"] = app._read_actual_position_inc(node, fresh=True)
                                if abs(result["settled_inc"] - target) > 2:
                                    result["status"] = "Settled endpoint outside 2-count window"
                            except Exception as exc:
                                result["status"] = f"Settled read failed: {exc}"
                    result["error_inc"] = result["settled_inc"] - target
                    rows.append(result)
                    writer.writerow(result)
                    f.flush()
                    (folder / "traces.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
                    app._post("test_result", result)
                    app._post("log", f"Repeatability {len(rows)}/{cycles * 2}: {result['status']}, "
                              f"endpoint error {result['error_inc']} counts")
                    if result["status"] != "OK":
                        raise RuntimeError(str(result["status"]))
            report["status"] = "COMPLETED"
    except Exception as exc:
        report["status"] = "STOPPED"
        report["reason"] = str(exc)
    finally:
        try:
            if app.__dict__.get("stop_pending", False):
                report["status"] = "STOPPED"
                report["shutdown_verification"] = "Pending in dedicated STOP worker; not confirmed by this report"
            else:
                app._disable_drive_worker()
                report["final_statusword"] = hex(app._read_statusword(node, fresh=True))
                report["final_error_register"] = int(node.sdo[0x1001].raw)
                report["final_position_inc"] = app._read_actual_position_inc(node, fresh=True)
                if int(report["final_statusword"], 16) & 0x6F == 0x27:
                    raise RuntimeError("Drive remains enabled")
                if report["final_error_register"]:
                    raise RuntimeError("Nonzero final error register")
        except Exception as exc:
            report["shutdown_error"] = str(exc)
            report["status"] = "STOPPED"
        report["completed_steps"] = len(rows)
        report["endpoints"] = summarize(rows)
        report["output_dir"] = str(folder.resolve())
        (folder / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        app._post("log", f"Repeatability {report['status']}: {folder}")
    return report
