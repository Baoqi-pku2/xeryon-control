"""Regression checks for stale feedback and terminal motion decisions; no hardware."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_gui import XeryonControlGui as Gui
from canopen.sdo.exceptions import SdoAbortedError, SdoCommunicationError


class DiagnosticSdo(dict):
    RESPONSE_TIMEOUT = 1.0
    MAX_RETRIES = 3


def make_app(position=20, timestamp=None):
    pdo = type("Pdo", (dict,), {})({"Position Actual Value": SimpleNamespace(raw=20),
                                    "Statusword": SimpleNamespace(raw=0x27)})
    pdo.timestamp = time.time() if timestamp is None else timestamp
    pdo.wait_for_reception = Mock()
    node = SimpleNamespace(id=32, tpdo={3: pdo}, state="OPERATION ENABLED", sdo={
        "Position Actual Value": SimpleNamespace(raw=position),
        "Statusword": SimpleNamespace(raw=0x27), 0x6040: SimpleNamespace(raw=0),
    })
    app = Gui.__new__(Gui)
    app.node = node
    app.nodes = {"A": node}
    app.active_axis_var = SimpleNamespace(get=lambda: "A")
    app.io_lock = threading.RLock()
    app.inc_per_mm = 1000
    app.active_target_inc = 100
    app.active_start_inc = 0
    app.active_target_time = time.monotonic() - 20
    app.active_target_deadline = time.monotonic() + 100
    app.active_best_remaining_inc = 80
    app.active_last_progress_time = time.monotonic() - 11
    app.runtime_position_window_inc = 2
    app.motion_trace_path = None
    app.test_stop_requested = threading.Event()
    app.status_busy = True
    app._post = Mock()
    app._read_mode = Mock(return_value="PROFILE_POSITION")
    app._write_controlword = Mock()
    def disable():
        app.active_target_inc = None
        app.active_start_inc = None
    app._disable_drive_worker = Mock(side_effect=disable)
    return app, node, pdo


class FeedbackTests(unittest.TestCase):
    def test_idle_manual_motion_publishes_live_counts_even_with_recent_unchanged_pdo(self):
        app, node, pdo = make_app()
        app.active_target_inc = None
        node.sdo["Statusword"].raw = pdo["Statusword"].raw = 0x1250
        for count in (13000, 13200):
            pdo.timestamp = time.time()
            node.sdo["Position Actual Value"].raw = count
            app._status_worker()
            status = [call.args[1] for call in app._post.call_args_list if call.args[0] == "status"][-1]
            self.assertEqual((status["position_inc"], status["position_um"]), (count, float(count)))
        self.assertEqual(pdo["Position Actual Value"].raw, 20)
        app._write_controlword.assert_not_called()

    def test_stale_pdo_is_not_position_or_status(self):
        app, node, _ = make_app(position=60, timestamp=time.time()-60)
        node.sdo["Statusword"].raw = 0x631
        self.assertEqual(app._read_actual_position_inc(node, fallback=20), 60)
        self.assertEqual(app._read_statusword(node, fallback=0x27), 0x631)

    def test_fresh_pdo_used_and_clock_jump_rejected(self):
        app, node, pdo = make_app(position=60)
        self.assertEqual(app._read_actual_position_inc(node), 20)
        pdo.timestamp = time.time()+60
        self.assertEqual(app._read_actual_position_inc(node), 60)

    def test_missing_sdo_never_returns_fallback(self):
        app, node, _ = make_app(timestamp=time.time()-60)
        del node.sdo["Position Actual Value"]
        with self.assertRaises(KeyError):
            app._read_actual_position_inc(node, fallback=20)

    def test_stall_candidate_live_progress_does_not_stop(self):
        app, _, _ = make_app(position=60)
        app._status_worker()
        app._disable_drive_worker.assert_not_called()
        self.assertEqual(app.active_best_remaining_inc, 40)
        self.assertEqual(app.active_target_inc, 100)
        self.assertFalse(app.status_busy)

    def test_true_stall_still_stops(self):
        app, _, _ = make_app(position=20)
        app._record_controller_snapshot = Mock()
        app._status_worker()
        app._disable_drive_worker.assert_called_once()
        app._record_controller_snapshot.assert_not_called()
        self.assertTrue(any("live SDO confirmed" in str(call) for call in app._post.call_args_list))

    def test_stale_target_reached_is_not_completion(self):
        app, node, pdo = make_app(position=50)
        pdo["Position Actual Value"].raw = 100
        pdo["Statusword"].raw = 0x427
        app._status_worker()
        app._disable_drive_worker.assert_not_called()
        self.assertEqual(app.last_position_inc, 50)
        self.assertEqual(app.active_best_remaining_inc, 50)

    def test_fault_has_priority_over_target_reached(self):
        app, node, pdo = make_app(position=100)
        pdo["Position Actual Value"].raw = 100
        pdo["Statusword"].raw = 0x408
        node.sdo["Statusword"].raw = 0x408
        app._status_worker()
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(any("驱动故障" in str(call) for call in app._post.call_args_list))
        self.assertFalse(any("Target reached at" in str(call) for call in app._post.call_args_list))

    def test_communication_failure_reports_unconfirmed_stop(self):
        app, node, _ = make_app(timestamp=time.time()-60)
        node.sdo.clear()
        app._status_worker()
        app._write_controlword.assert_called_once_with(node, 0x00)
        self.assertIsNone(app.active_target_inc)
        self.assertTrue(any("停止状态未确认" in str(call) for call in app._post.call_args_list))

    def test_shortcut_completion_is_posted_after_verified_disable(self):
        app, node, pdo = make_app(position=100)
        pdo["Position Actual Value"].raw = 100
        pdo["Statusword"].raw = 0x427
        node.sdo["Statusword"].raw = 0x427
        app.shortcut_active = {"generation": 7}
        app._status_worker()
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(any(call.args == ("shortcut_done", 7) for call in app._post.call_args_list))

    def test_disable_confirmation_failure_cancels_shortcut_without_done(self):
        app, node, pdo = make_app(position=100)
        pdo["Position Actual Value"].raw = 100
        pdo["Statusword"].raw = 0x427
        node.sdo["Statusword"].raw = 0x427
        app.shortcut_active = {"generation": 7}
        def failed_disable():
            app.active_target_inc = None
            app.active_start_inc = None
            raise RuntimeError("disable not confirmed")
        app._disable_drive_worker = Mock(side_effect=failed_disable)
        app._status_worker()
        self.assertFalse(any(call.args == ("shortcut_done", 7) for call in app._post.call_args_list))
        self.assertTrue(any(call.args[0] == "shortcut_failed" for call in app._post.call_args_list))

    def test_trace_contains_command_position_and_stop(self):
        app, node, _ = make_app()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory, patch.dict("os.environ", {"LOCALAPPDATA": directory}):
            app._start_motion_trace(node, 0, 100, {"velocity":5})
            app._status_worker()
            events = [json.loads(line) for line in app.motion_trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["target_inc"], 100)
            self.assertEqual(events[-1]["reason"], "no_progress_live_confirmed")
            self.assertEqual(events[-1]["position_inc"], 20)

    def test_controller_trace_preserves_demand_and_target_without_writes(self):
        app, node, _ = make_app()
        node.sdo = DiagnosticSdo({idx: SimpleNamespace(raw=value) for idx, value in
            ((0x6062, 40), (0x6064, 20), (0x60F4, 20), (0x607A, 100), (0x1001, 0), (0x1002, 0))})
        app.motion_trace_path = Path("unused")
        app.controller_trace_next_time = 0
        app.controller_trace_unsupported = set()
        app._record_motion_event = Mock()
        app._record_controller_snapshot(node)
        values = app._record_motion_event.call_args.kwargs
        self.assertEqual((values["demand_inc"], values["actual_inc"], values["target_inc"]), (40, 20, 100))
        self.assertEqual(values["following_error_inc"], 20)
        self.assertEqual((node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES), (1.0, 3))
        app._record_controller_snapshot(node)
        app._record_motion_event.assert_called_once()
        app._write_controlword.assert_not_called()

    def test_controller_read_failure_restores_sdo_and_uses_fault_stop(self):
        app, node, _ = make_app(position=60)
        class FailedSdo(DiagnosticSdo):
            def __getitem__(self, index):
                if index == 0x6062:
                    raise SdoCommunicationError("lost link")
                return super().__getitem__(index)
        node.sdo = FailedSdo(node.sdo)
        app.motion_trace_path = Path("unused")
        app.controller_trace_next_time = 0
        app.controller_trace_unsupported = set()
        app._record_motion_event = Mock()
        app._status_worker()
        self.assertIsNone(app.active_target_inc)
        app._write_controlword.assert_called_once_with(node, 0x00)
        self.assertEqual((node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES), (1.0, 3))
        self.assertTrue(any(c.args[0] == "communication_fault" for c in app._record_motion_event.call_args_list))

    def test_unsupported_diagnostic_object_does_not_fake_zero(self):
        app, node, _ = make_app()
        class PartialSdo(DiagnosticSdo):
            def __getitem__(self, index):
                if index == 0x60F4:
                    raise SdoAbortedError(0x06020000)
                return SimpleNamespace(raw=20)
        node.sdo = PartialSdo()
        app.motion_trace_path = Path("unused")
        app.controller_trace_next_time = 0
        app.controller_trace_unsupported = set()
        app._record_motion_event = Mock()
        app._record_controller_snapshot(node)
        values = app._record_motion_event.call_args.kwargs
        self.assertNotIn("following_error_inc", values)
        self.assertIn("following_error_inc_unavailable", values)
        self.assertIn(0x60F4, app.controller_trace_unsupported)


if __name__ == "__main__":
    unittest.main()
