"""Explicitly invoked, small-travel unloaded test using the GUI motion backend."""

import argparse
import json
from pathlib import Path

from xeryon_control.ui.main_window import XeryonControlGui
from xeryon_control.application.repeatability import run_repeatability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-unloaded-centered", action="store_true", required=True)
    parser.add_argument("--channel", default="COM3")
    parser.add_argument("--node", type=int, default=32)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--span-counts", type=int, default=10)
    parser.add_argument("--encoder", choices=["10MU", "1MU", "250NAN", "100NAN"], default="1MU")
    parser.add_argument("--encoder-label-verified", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    args = parser.parse_args()
    app = XeryonControlGui()
    app.withdraw()
    report = None
    try:
        app.channel_var.set(args.channel)
        app.node_id_var.set(str(args.node))
        app.encoder_var.set(args.encoder)
        app._connect_worker(app._connection_snapshot())
        node = app._require_node()
        if int(node.sdo[0x1001].raw):
            raise RuntimeError("Nonzero device error; no test started")
        report = run_repeatability(app, {
            "cycles": args.cycles, "span_inc": args.span_counts, "speed_inc_s": 5,
            "axis": "Actuator A", "profile": app._profile_snapshot(),
            "encoder": args.encoder, "encoder_verified": args.encoder_label_verified,
            "output_dir": str(args.output),
        })
        print(json.dumps(report, indent=2), flush=True)
    finally:
        app._disconnect_worker()
        app.destroy()
    return 0 if report and report["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
