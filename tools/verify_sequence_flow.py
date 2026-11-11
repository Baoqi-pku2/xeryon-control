"""Real GUI/worker/STOP pipeline with a simulated transport; never opens CAN.

Motion is idealized and accelerated. Dwell uses real monotonic seconds. This
checks host sequencing, not firmware trajectories or physical performance.
"""
import argparse
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from xeryon_control.ui.main_window import XeryonControlGui
from xeryon_control.domain.shortcut_presets import SHORTCUT_PRESETS

if __package__:
    from .simulated_node import SimulatedNode
else:  # Direct script invocation from the repository.
    from simulated_node import SimulatedNode


def run(output, dwell=30.0):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"hardware_motion": False, "transport": "idealized simulator",
              "motion_time_acceleration": 5000, "dwell_seconds_real_time": dwell,
              "start_clicks": 0, "completed_steps": [], "errors": []}
    with patch.dict(os.environ, {"LOCALAPPDATA": str(output.parent)}), \
            patch("xeryon_control.ui.main_window.setup_network", side_effect=AssertionError("CAN access forbidden")):
        app = XeryonControlGui()
        app.withdraw()
        app.update_idletasks()
        # No network, node setup or device discovery is performed.
        node = SimulatedNode()
        app.nodes = {"Actuator A": node}
        app.node = node
        origin_um = app._inc_to_um(node.position)
        app.position_panel.apply_user_zero(origin_um, time.monotonic())
        app.position_panel.apply_travel()  # Exercise the previously failing precheck.
        app.shortcut_dwell_var.set(str(dwell))
        original_done = app._handle_shortcut_done

        def on_done(generation):
            if app.shortcut_active is not None and app.shortcut_wait_deadline is None:
                report["completed_steps"].append({"number": app.shortcut_active["number"],
                    "time": time.monotonic(), "position_inc": node.read(0x6064),
                    "disabled": node.read(0x6041) & 0x4F == 0x40})
            original_done(generation)
        app._handle_shortcut_done = on_done
        app._show_error = lambda message: report["errors"].append(str(message))
        started = time.monotonic()
        deadline = started + 6 * dwell + 40

        def start():
            for number in range(1, 8):
                app.position_panel.shortcut_buttons[number].invoke()
            report["start_clicks"] += 1
            app.position_panel.start_button.invoke()

        def watch():
            done = len(report["completed_steps"]) == 7 and app.shortcut_active is None
            failed = bool(report["errors"]) or time.monotonic() >= deadline
            if (done or failed) and not app.command_busy and not app.status_busy and not app.stop_pending:
                report["elapsed_s"] = time.monotonic() - started
                report["commands"] = node.commands
                report["final_user_um"] = app._inc_to_um(node.read(0x6064)) - origin_um
                report["final_disabled"] = node.read(0x6041) & 0x4F == 0x40
                report["ui_status"] = app.shortcut_status_var.get()
                report["inter_step_intervals_s"] = [node.commands[i+1]["time"]-step["time"]
                    for i, step in enumerate(report["completed_steps"][:-1])
                    if i+1 < len(node.commands)]
                expected = [app._um_to_inc(origin_um + preset[2]) for preset in SHORTCUT_PRESETS]
                report["passed"] = (done and not failed and report["start_clicks"] == 1
                    and [cmd["target_inc"] for cmd in node.commands] == expected
                    and all(step["disabled"] for step in report["completed_steps"])
                    and len(report["inter_step_intervals_s"]) == 6
                    and all(value >= dwell for value in report["inter_step_intervals_s"])
                    and abs(report["final_user_um"]-50) <= app._inc_to_um(1)
                    and report["final_disabled"])
                (output.parent/"sequence_ui_log.txt").write_text(app.log_text.get("1.0", "end"), encoding="utf-8")
                for timer in app.tk.call("after", "info"):
                    app.after_cancel(timer)
                app.destroy()
                return
            app.after(100, watch)

        app.after(50, start)
        app.after(100, watch)
        app.mainloop()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dwell", type=float, default=30)
    args = parser.parse_args()
    result = run(args.output, args.dwell)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
