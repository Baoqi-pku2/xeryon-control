"""Offline Tk queue stress check. No node or network is created."""
import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from control_gui import XeryonControlGui


def measure():
    app = XeryonControlGui()
    app.withdraw()
    app.update_idletasks()
    started = time.perf_counter()
    beats = [started]
    result = {"hardware_connected": False, "queued_logs": 5000, "stop_called": False}
    for index in range(result["queued_logs"]):
        app._post("log", f"Synthetic UI stress event {index}")

    def heartbeat():
        beats.append(time.perf_counter())
        app.after(20, heartbeat)

    stop_due = time.perf_counter() + 0.15
    def stop():
        result["stop_callback_delay_ms"] = (time.perf_counter() - stop_due) * 1000
        app.stop_actuator_test()
        result["stop_called"] = app.test_stop_requested.is_set()

    def finish_when_drained():
        elapsed = time.perf_counter() - started
        if (app.ui_queue.empty() and result["stop_called"]) or elapsed >= 10:
            gaps = sorted((right - left) * 1000 for left, right in zip(beats, beats[1:]))
            result.update(elapsed_s=elapsed, remaining_events=app.ui_queue.qsize(),
                          heartbeat_max_gap_ms=max(gaps), heartbeat_p95_gap_ms=gaps[int((len(gaps)-1)*0.95)],
                          visible_log_lines=int(app.log_text.index("end-1c").split(".")[0]) - 1)
            app.destroy()
        else:
            app.after(50, finish_when_drained)

    app.after(20, heartbeat)
    app.after(150, stop)
    app.after(200, finish_when_drained)
    # Any accidental transport creation is an immediate verification failure.
    with patch("control_gui.setup_network", side_effect=AssertionError("Offline check attempted CAN")):
        app.mainloop()
    result["passed"] = (result["stop_called"] and result["remaining_events"] == 0
                        and result["heartbeat_max_gap_ms"] < 250
                        and result["stop_callback_delay_ms"] < 250
                        and result["visible_log_lines"] <= 2000)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = measure()
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
