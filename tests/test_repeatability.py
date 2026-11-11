import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from xeryon_control.application.repeatability import run_repeatability, summarize


class FakeApp:
    def __init__(self, status="OK"):
        raw = type("Raw", (), {"raw": 0})()
        self.node = type("Node", (), {"id": 32, "sdo": {0x1018: {4: raw}, 0x1001: raw}})()
        self.position = 100
        self.targets = []
        self.status = status
        self.disabled = False
        self.test_stop_requested = threading.Event()

    def _require_node(self): return self.node
    def _read_actual_position_inc(self, *a, **kw): return self.position
    def _read_statusword(self, *a, **kw): return 0x631
    def _inc_to_um(self, value): return value * 1.0079
    def _post(self, *a): pass
    def _disable_drive_worker(self): self.disabled = True

    def _run_test_move(self, node, axis, step, speed, distance, profile, timeout, **kwargs):
        target = kwargs["absolute_target_inc"]
        self.targets.append(target)
        start = self.position
        # A repeatable one-count offset must not accumulate into the next target.
        self.position = target + 1
        return dict(step=step, start_inc=start, target_inc=target, end_inc=self.position,
                    status=self.status, duration=1)


class RepeatabilityTests(unittest.TestCase):
    def config(self, output):
        return dict(cycles=3, span_inc=10, speed_inc_s=5, profile={}, axis="A",
                    encoder="1MU", encoder_verified=False, output_dir=output)

    @patch("threading.Event.wait", return_value=False)
    def test_fixed_targets_and_statistics(self, _):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            app = FakeApp()
            report = run_repeatability(app, self.config(folder))
            self.assertEqual(app.targets, [110, 100] * 3)
            self.assertTrue(app.disabled)
            self.assertEqual(report["status"], "COMPLETED")
            self.assertEqual(report["endpoints"]["100"]["mean_error_inc"], 1)
            self.assertEqual(report["endpoints"]["100"]["peak_to_peak_inc"], 0)
            self.assertFalse(report["encoder_label_verified"])
            self.assertEqual(json.loads((Path(report["output_dir"]) / "summary.json").read_text())["completed_steps"], 6)

    def test_failure_does_not_return_or_continue(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            app = FakeApp("Following error")
            report = run_repeatability(app, self.config(folder))
            self.assertEqual(app.targets, [110])
            self.assertTrue(app.disabled)
            self.assertEqual(report["status"], "STOPPED")
            self.assertEqual(report["endpoints"], {})

    def test_summary_excludes_failed_steps(self):
        rows = [dict(status="OK", target_inc=10, settled_inc=x) for x in (9, 10, 11)]
        rows.append(dict(status="Timeout", target_inc=10, settled_inc=99))
        result = summarize(rows)["10"]
        self.assertEqual(result, dict(n=3, mean_error_inc=0, sample_sd_inc=1, peak_to_peak_inc=2))


if __name__ == "__main__":
    unittest.main()
