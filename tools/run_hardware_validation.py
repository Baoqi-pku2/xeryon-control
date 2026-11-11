"""User-authorized unloaded actuator checks through the actual GUI pipeline.

Reference must be the operator-confirmed retracted endpoint count.
No flash writes, homing, automatic retry or fault bypass. STOP/Esc remain live.
"""
import argparse
import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from xeryon_control.ui.main_window import XeryonControlGui
from xeryon_control.ui import main_window as control_gui
from xeryon_control.domain import shortcut_presets


def apply_verified_shutdown(report, shutdown_completed, verified_stops):
    """UI position may predate an enable failure; only claim a verified stop."""
    report['shutdown_completed'] = shutdown_completed
    if shutdown_completed and 33 in verified_stops:
        report['last_ui_position_inc'] = report.get('final_position_inc')
        report['final_position_inc'] = verified_stops[33]['position_inc']
        report['final_statusword'] = verified_stops[33]['statusword']
        report['final_position_source'] = 'verified_stop_before_disconnect'
    else:
        report['last_ui_position_inc'] = report.get('final_position_inc')
        report['final_position_inc'] = None
        report['final_position_source'] = 'unverified'
    if report['result'] == 'PASSED' and (not shutdown_completed or 33 not in verified_stops):
        report['result'] = 'SHUTDOWN_UNCONFIRMED'


def run(phase, output, speed, acceleration, targets_user=None, tick_ms=100, reference_count=-143):
    sequence = phase in ("sequence", "sequence_short")
    if phase == "sequence_short":
        # Diagnostic input data only: keep the actual GUI, CAN transport,
        # command worker, arrival checks and cancellation code unchanged.
        presets = tuple((i, f"空载短程验证 {i}", target, speed)
                        for i, target in enumerate((100, 150, 200, 250, 150, 100, 50), 1))
        control_gui.SHORTCUT_PRESETS = presets
        shortcut_presets.SHORTCUT_PRESETS = presets
    output = output.resolve()
    if output.exists() or output.with_suffix('.events.jsonl').exists():
        raise ValueError('Use a new output path; previous evidence must not be overwritten')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.harness.py').write_bytes(Path(__file__).read_bytes())
    os.environ["LOCALAPPDATA"] = str(output.parent)
    app = XeryonControlGui()
    app.title(app.title() + " · 空载实机验证 / STOP 可中止")
    app.channel_var.set("COM3")
    app.node_id_var.set("33")
    app.reset_on_connect_var.set(False)
    app.node_b_enabled_var.set(False)
    app.encoder_var.set("1MU")
    app.shortcut_dwell_var.set("30")
    app.acceleration_var.set(str(acceleration))
    app.max_velocity_var.set(str(max(float(app.max_velocity_var.get()), speed)))
    app.max_acceleration_var.set(str(max(500, acceleration)))
    report = {"phase": phase, "reference_count": reference_count, "speed_um_s": speed,
              "acceleration_um_s2": acceleration, "hardware_motion": True,
              "errors": [], "steps": [], "start_clicks": 0}
    report["source_sha256"] = hashlib.sha256((ROOT / "src/xeryon_control/ui/main_window.py").read_bytes()).hexdigest()
    report['harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    verified_stops = {}
    original_record = app._record_motion_event
    def record(event, **values):
        original_record(event, **values)
        if event == 'stop_verified':
            verified_stops[values['node_id']] = dict(values)
        if event in ('stop_verified', 'position_reference_changed', 'unexpected_displacement', 'command', 'terminal_live'):
            with output.with_suffix('.events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(event=event,monotonic_s=time.monotonic(),**values),ensure_ascii=False)+'\n')
    app._record_motion_event = record
    if sequence:
        report["presets"] = control_gui.SHORTCUT_PRESETS
        report["dwell_seconds"] = 30
        report["submissions"] = []
    started = time.monotonic()
    state = "connecting"
    targets = []
    next_step = 0
    finished = False
    total_deadline = 3100 if phase == "sequence" else 360 if phase == "sequence_short" else 650 if phase == "target50" else 180
    original_error = app._show_error
    original_done = app._handle_shortcut_done
    original_start_step = app._start_shortcut_step

    def start_step(step):
        report["submissions"].append({"number": step["number"], "elapsed_s": time.monotonic()-started})
        return original_start_step(step)
    if sequence:
        app._start_shortcut_step = start_step

    def error(message):
        report["errors"].append(str(message))
        original_error(message)
    app._show_error = error

    def sequence_done(generation):
        if app.shortcut_active and app.shortcut_wait_deadline is None:
            report["steps"].append({"number": app.shortcut_active["number"],
                                    "elapsed_s": time.monotonic()-started,
                                    "position_inc": app.last_position_inc})
        original_done(generation)
    app._handle_shortcut_done = sequence_done

    def save(result):
        report.update(result=result, elapsed_s=time.monotonic()-started,
                      target_status=app.target_status_var.get(), sequence_status=app.shortcut_status_var.get(),
                      final_position_inc=app.last_position_inc, stop_unconfirmed=app.stop_unconfirmed)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        output.with_suffix(".log").write_text(app.log_text.get("1.0", "end"), encoding="utf-8")

    def finish(result):
        nonlocal finished
        if finished:
            return
        finished = True
        save(result)
        # Use the same verified close/disable/disconnect path as the product.
        app._on_close()

    def submit_local():
        nonlocal next_step, state
        target_inc = targets[next_step]
        app.position_panel.user_target.set(repr(app._inc_to_um(target_inc - reference_count)))
        app.position_panel.speed.set(str(speed))
        app.position_panel.redraw()
        report["start_clicks"] += 1
        app.position_panel.start_button.invoke()
        report["steps"].append({"target_inc": target_inc, "submitted_s": time.monotonic()-started})
        next_step += 1
        state = "local_moving"

    def tick():
        nonlocal state, targets, total_deadline
        if finished:
            return
        elapsed = time.monotonic() - started
        if elapsed > total_deadline:
            report["errors"].append("Verification deadline exceeded")
            app.stop_actuator_test()
            finish("TIMEOUT")
            return
        if report["errors"] and not app.command_busy:
            finish("FAILED")
            return
        idle = not app.command_busy and not app.status_busy and app.ui_queue.empty()
        if state == "connecting" and app.node is not None and idle and app.position_panel.is_fresh():
            # Reference was explicitly confirmed by the operator, never inferred
            # from the current (extended) position or written into the encoder.
            current = app.last_position_inc
            app.position_panel.apply_user_zero(app._inc_to_um(reference_count), time.monotonic())
            app._set_status({"axis": "Actuator A", "position_um": app._inc_to_um(current),
                            "sample_time": time.monotonic(), "statusword": app.last_statusword,
                            "state": app.state_var.get(), "mode": app.mode_var.get()})
            report["initial_position_inc"] = current
            app.position_panel.apply_travel()
            if sequence:
                for number in range(1, 8):
                    app.position_panel.shortcut_buttons[number].invoke()
                report["start_clicks"] += 1
                app.position_panel.start_button.invoke()
                state = "sequence_moving"
            else:
                # Both targets must pass the application's real travel/default
                # limits. No callbacks or safety methods are mocked.
                targets = ([app._um_to_inc(app._inc_to_um(reference_count) + float(value)) for value in targets_user]
                           if targets_user is not None else [app._um_to_inc(app._inc_to_um(reference_count) + 50)]
                           if phase == "target50" else [current + 100, current])
                if targets_user is not None:
                    distance = sum(abs(b-a) for a,b in zip([current]+targets, targets)) * app._inc_to_um(1)
                    total_deadline = elapsed + (distance/speed + len(targets)*(speed/acceleration+5))*1.5+60
                    report['targets_user_um'] = targets_user
                submit_local()
        elif state == "local_moving" and idle and app.active_target_inc is None:
            status = app.target_status_var.get()
            if status.startswith("已到达"):
                report["steps"][-1].update(completed_s=elapsed, end_inc=app.last_position_inc, status=status)
                if next_step < len(targets):
                    submit_local()
                else:
                    finish("PASSED")
                    return
            else:
                report["errors"].append(status)
                finish("FAILED")
                return
        elif state == "sequence_moving" and idle and app.shortcut_active is None:
            intervals = [report["submissions"][i+1]["elapsed_s"]-step["elapsed_s"]
                         for i, step in enumerate(report["steps"])
                         if i+1 < len(report["submissions"])]
            report["inter_step_intervals_s"] = intervals
            finish("PASSED" if len(report["steps"]) == 7 and len(intervals) == 6
                   and all(value >= 30 for value in intervals) else "FAILED")
            return
        app.after(tick_ms, tick)

    app.after(100, app.connect_device)
    app.after(200, tick)
    app.mainloop()
    if not finished:
        # User close is not successful completion of a test.
        report.update(result="CLOSED_BY_USER", elapsed_s=time.monotonic()-started)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_verified_shutdown(report, app.node is None and not app.stop_unconfirmed, verified_stops)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("local", "target50", "sequence", "sequence_short", "targets"), required=True)
    parser.add_argument("--targets", nargs='+', type=float)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speed", type=float, default=5)
    parser.add_argument("--acceleration", type=float, default=50)
    parser.add_argument("--reference-count", type=int, default=-143,
                        help="Operator-confirmed physical retracted endpoint count; never infer it from an arbitrary position")
    args = parser.parse_args()
    if args.phase == 'targets' and not args.targets:
        parser.error('--phase targets requires --targets')
    result = run(args.phase, args.output, args.speed, args.acceleration, args.targets, reference_count=args.reference_count)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result["result"] == "PASSED" else 1)
