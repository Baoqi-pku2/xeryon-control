"""Sequence dwell, real-resolution edge geometry, and fault rejection. No CAN."""
import time
import unittest
from tests.qt_support import create_console
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from xeryon_control.application.session import ControllerSession as Gui
from xeryon_control.domain.travel import TravelRange
from tests.test_feedback import make_app


class SequenceGeometryTests(unittest.TestCase):
    def test_fifty_um_from_401_real_encoder_origins(self):
        resolution = 1.0079
        for origin in range(-200, 201):
            with self.subTest(origin=origin):
                zero = origin * resolution
                travel = TravelRange()
                travel.confirm(zero + 30000, 50, "A")
                requested = zero + 50
                count = round(requested / resolution)
                travel.validate(requested, count, resolution, "A")
                self.assertAlmostEqual(count * resolution - zero, 50.395, places=8)
                with self.assertRaises(ValueError):
                    travel.validate(requested - 0.001, count, resolution, "A")
                with self.assertRaises(ValueError):
                    travel.validate(requested, count - 1, resolution, "A")

    def test_live_overshoot_cancels_sequence_before_stall_timeout(self):
        app, node, pdo = make_app(position=120)
        pdo["Position Actual Value"].raw = 120
        app.active_last_progress_time = time.monotonic()
        app.shortcut_active = {"generation": 1}
        app._status_worker()
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(any(c.args[0] == "shortcut_failed" for c in app._post.call_args_list))
        self.assertFalse(any(c.args[0] == "shortcut_done" for c in app._post.call_args_list))

    def test_cached_overshoot_is_rechecked_against_live_device(self):
        app, node, pdo = make_app(position=95)
        pdo["Position Actual Value"].raw = 120
        app._status_worker()
        app._disable_drive_worker.assert_not_called()
        self.assertEqual(app.last_position_inc, 95)

    def test_dwell_fault_or_drift_cancels_without_another_start(self):
        for position, status in ((120, 0x40), (100, 0x08)):
            app, node, pdo = make_app(position=position)
            node.sdo["Statusword"].raw = status
            pdo["Statusword"].raw = status
            app.active_target_inc = None
            app.active_start_inc = None
            app.shortcut_wait_deadline = time.monotonic() + 30
            app.shortcut_active = {"generation": 1, "target": 100}
            app._status_worker()
            app._disable_drive_worker.assert_called_once()
            self.assertTrue(app.test_stop_requested.is_set())
            self.assertTrue(any(c.args[0] == "shortcut_failed" for c in app._post.call_args_list))


class SequenceDwellTests(unittest.TestCase):
    def setUp(self):
        self.window = create_console()
        self.app = self.window.session
        self.node = NS(id=33)
        self.app.node = self.node
        self.app.nodes = {"Actuator A": self.node}
        self.app.position_panel.apply_user_zero(-196 * 1.0079, time.monotonic())
        self.app._safe_start = Mock(return_value=True)
        self.app._show_error = Mock()

    def tearDown(self):
        self.window.finish_exit()
        self.window.deleteLater()

    def start_pair(self):
        self.app.toggle_shortcut(1)
        self.app.toggle_shortcut(5)
        self.app.start_position_panel_move()
        self.app._show_error.assert_not_called()
        generation = self.app.shortcut_generation
        self.app._handle_shortcut_done(generation)
        return generation

    def test_default_thirty_second_dwell_then_automatic_next(self):
        self.assertEqual(self.app.shortcut_dwell_var.get(), "30")
        generation = self.start_pair()
        self.app._safe_start.assert_called_once()
        self.assertEqual(self.app.shortcut_active["number"], 1)
        deadline = self.app.shortcut_wait_deadline
        self.app._handle_shortcut_done(generation)
        self.assertEqual(self.app.shortcut_wait_deadline, deadline)
        with patch("xeryon_control.application.session.time.monotonic", return_value=deadline - 0.01):
            self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        with patch("xeryon_control.application.session.time.monotonic", return_value=deadline + 0.01), \
                patch.object(self.app.position_panel, "is_fresh", return_value=True):
            self.app._continue_shortcut_after_dwell(generation)
        self.assertEqual(self.app._safe_start.call_count, 2)
        self.assertEqual(self.app.shortcut_active["number"], 5)
        self.app._handle_shortcut_done(generation)
        self.assertIsNone(self.app.shortcut_active)
        self.assertIsNone(self.app.shortcut_wait_deadline)

    def test_stop_during_dwell_never_starts_next(self):
        generation = self.start_pair()
        with patch.object(self.app, "_run_stop_command"):
            self.app.stop_actuator_test()
        self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        self.assertEqual(self.app.shortcut_pending, [])
        self.assertIsNone(self.app.shortcut_wait_timer)

    def test_expired_feedback_at_dwell_end_aborts_after_bounded_refresh(self):
        generation = self.start_pair()
        self.app.shortcut_wait_deadline = 0
        self.app.position_panel.received_at = None
        self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        self.app._show_error.assert_not_called()
        self.app.shortcut_feedback_wait_started = time.monotonic() - 2.1
        self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        self.app._show_error.assert_called_once()
        self.assertIsNone(self.app.shortcut_active)

    def test_lost_link_failure_during_dwell_cancels_timer(self):
        generation = self.start_pair()
        self.app._handle_shortcut_failed({"generation": generation, "reason": "通信故障"})
        self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        self.assertIsNone(self.app.shortcut_wait_timer)

    def test_failed_submission_cannot_leave_a_stuck_sequence(self):
        self.app._safe_start.return_value = False
        self.app.toggle_shortcut(1)
        self.app.start_selected_shortcuts()
        self.assertIsNone(self.app.shortcut_active)
        self.app._show_error.assert_called_once()

    def test_busy_worker_at_dwell_end_waits_then_continues(self):
        generation = self.start_pair()
        self.app.shortcut_wait_deadline = 0
        self.app.command_busy = True
        self.app._continue_shortcut_after_dwell(generation)
        self.app._safe_start.assert_called_once()
        self.app.command_busy = False
        self.app._continue_shortcut_after_dwell(generation)
        self.assertEqual(self.app._safe_start.call_count, 2)

    def test_position_precheck_accepts_real_retracted_endpoint(self):
        self.app.position_panel.apply_travel()
        self.app._show_error.assert_not_called()
        self.assertTrue(self.app.travel_range.confirmed)

    def test_invalid_dwell_rejected_before_any_motion(self):
        for value in ("", "-1", "nan", "inf", "100000"):
            self.app.shortcut_dwell_var.set(value)
            with self.assertRaises(ValueError):
                self.app._shortcut_snapshot(1)
        self.app._safe_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
