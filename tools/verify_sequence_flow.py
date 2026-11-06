"""Real GUI/worker/STOP pipeline with a simulated transport; never opens CAN.

Motion is idealized and accelerated. Dwell uses real monotonic seconds. This
checks host sequencing, not firmware trajectories or physical performance.
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from control_gui import XeryonControlGui
from shortcut_presets import SHORTCUT_PRESETS


class Cell:
    def __init__(self, node, index):
        self.node, self.index = node, index

    @property
    def raw(self):
        return self.node.read(self.index)

    @raw.setter
    def raw(self, value):
        self.node.write(self.index, int(value))


class Sdo:
    RESPONSE_TIMEOUT = 1.0
    MAX_RETRIES = 1
    PAUSE_BEFORE_SEND = 0
    names = {"Controlword": 0x6040, "Statusword": 0x6041, "Position Actual Value": 0x6064,
             "Mode of operation": 0x6060, "Modes of Operation Display": 0x6061,
             "Profile velocity": 0x6081, "Profile acceleration": 0x6083,
             "Max profile velocity": 0x607F, "Max acceleration": 0x60C5,
             "Quick stop deceleration": 0x6085, "Following error window": 0x6065,
             "Following error time out": 0x6066, "Position window": 0x6067,
             "Position window time": 0x6068}

    def __init__(self, node):
        self.node = node

    def __getitem__(self, key):
        if key == 0x60A4:
            return {1: Cell(self.node, (0x60A4, 1))}
        if key == "Software position limit":
            return {1: Cell(self.node, (0x607D, 1)), 2: Cell(self.node, (0x607D, 2))}
        return Cell(self.node, self.names[key] if isinstance(key, str) else key)


class Rpdo(dict):
    def __init__(self, node):
        super().__init__({"Controlword": NS(raw=0), "Target Position": NS(raw=node.position)})
        self.node = node

    def transmit(self):
        self.node.write(0x607A, self["Target Position"].raw)
        self.node.write(0x6040, self["Controlword"].raw)


class SimulatedNode:
    def __init__(self, origin=-196):
        self.id = 33
        self.position = origin
        self.registers = {0x6040: 0, 0x6041: 0x40, 0x607A: origin, 0x6060: 1, 0x6081: 5}
        self.lock = threading.RLock()
        self.trajectory = None
        self.commands = []
        self.sdo = Sdo(self)
        self.rpdo = {3: Rpdo(self)}
        self.tpdo = {3: NS(timestamp=None, wait_for_reception=lambda **kwargs: None)}

    @property
    def state(self):
        return "OPERATION ENABLED" if self.read(0x6041) & 0x6F == 0x27 else "SWITCH ON DISABLED"

    def tick(self):
        if self.trajectory is None:
            return
        start, target, started, duration = self.trajectory
        fraction = min(1, (time.monotonic() - started) / duration)
        self.position = round(start + (target - start) * fraction)
        if fraction >= 1:
            self.trajectory = None
            self.registers[0x6041] = 0x427 | (self.registers[0x6041] & 0x1000)

    def read(self, index):
        with self.lock:
            self.tick()
            if index in (0x6064, 0x6062):
                return self.position
            if index == 0x6061:
                return self.registers[0x6060]
            return self.registers.get(index, 0)

    def write(self, index, value):
        with self.lock:
            self.tick()
            previous = self.registers.get(index, 0)
            self.registers[index] = value
            if index != 0x6040:
                return
            if value in (0, 6, 7, 11):
                self.trajectory = None
                self.registers[0x6041] = {0: 0x40, 6: 0x21, 7: 0x23, 11: 0x07}[value]
            elif value & 15 == 15:
                if self.registers[0x6041] & 0x6F != 0x27:
                    self.registers[0x6041] = 0x27
                if value & 16 and not previous & 16:
                    target = self.registers[0x607A]
                    velocity = self.registers[0x6081]
                    duration = max(0.05, min(1, abs(target-self.position) / velocity / 5000))
                    self.trajectory = (self.position, target, time.monotonic(), duration)
                    self.registers[0x6041] = 0x27
                    self.commands.append({"time": time.monotonic(), "target_inc": target,
                                          "velocity_inc": velocity, "start_inc": self.position})
                if value & 16:
                    self.registers[0x6041] |= 0x1000
                else:
                    self.registers[0x6041] &= ~0x1000


def run(output, dwell=30.0):
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"hardware_motion": False, "transport": "idealized simulator",
              "motion_time_acceleration": 5000, "dwell_seconds_real_time": dwell,
              "start_clicks": 0, "completed_steps": [], "errors": []}
    with patch.dict(os.environ, {"LOCALAPPDATA": str(output.parent)}), \
            patch("control_gui.setup_network", side_effect=AssertionError("CAN access forbidden")):
        app = XeryonControlGui()
        app.withdraw()
        app.update_idletasks()
        # No network, node setup or device discovery is performed.
        node = SimulatedNode()
        app.nodes = {"Actuator A": node}
        app.node = node
        app.encoder_verified_var.set(True)
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
