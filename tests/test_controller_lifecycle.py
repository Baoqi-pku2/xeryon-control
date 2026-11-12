"""Safety regressions: synthetic transport and real Qt widgets, never opens a CAN bus."""
import tempfile
import threading
import unittest
from tests.qt_support import create_console
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from xeryon_control.application.session import ControllerSession as Gui
from tests.test_hardening import make_app, linked_app, var
from tests.test_stop_safety import node as fake_node
from tests.test_repeatability import FakeApp
from xeryon_control.application.repeatability import run_repeatability
from xeryon_control.hardware.stop import verify_disabled


class ControllerLifecycleTests(unittest.TestCase):
    def test_device_limits_round_inward_for_single_and_inverted_axes(self):
        app = linked_app()
        for node in app.nodes.values():
            node.sdo = {"Software position limit": {1: NS(raw=0), 2: NS(raw=0)}}
        limits = {"enabled": True, "min": 0.6, "max": 100.8}
        app._linked_snapshot = Mock(return_value={"enabled": False})
        app._apply_limits_worker(limits)
        values = app.node.sdo["Software position limit"]
        self.assertEqual((values[1].raw, values[2].raw), (1, 100))
        app._apply_linked_limits_worker(limits, {"enabled": True, "invert_b": True,
            "offsets": {"Actuator A": 1000, "Actuator B": 2000}})
        values = app.nodes["Actuator B"].sdo["Software position limit"]
        self.assertEqual((values[1].raw, values[2].raw), (1900, 1999))

    def test_real_stop_thread_verifies_single_axis_once(self):
        app = make_app()
        app.node = fake_node()
        app.nodes = {"Actuator A": app.node}
        app._validate_target = Mock()
        app._prepare_profile_position = Mock()
        app._command_profile_position = Mock()
        app._read_actual_position_inc = Mock(return_value=100)
        app._write_controlword = Mock()
        stopper = None
        def request_stop(*args, **kwargs):
            nonlocal stopper
            app.stop_pending = True
            app.cancel_generation += 1
            app.test_stop_requested.set()
            stopper = threading.Thread(target=app._stop_runner, args=("STOP",))
            stopper.start()
            return 0x27
        app._read_statusword = Mock(side_effect=request_stop)
        with patch("xeryon_control.application.stop.verify_disabled", wraps=verify_disabled) as verified:
            with app.io_lock:
                result = app._run_test_move(app.node, "Actuator A", 1, 5, 10,
                    {"max_velocity": 100, "limits": {"enabled": False}}, 1)
            stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(result["status"], "Stopped")
        verified.assert_called_once_with(app.node)
        self.assertFalse(app.stop_unconfirmed)
        self.assertFalse(app.stop_pending)

    def test_repeatability_stop_report_does_not_claim_verification(self):
        app = FakeApp()
        app.stop_pending = True
        app.test_stop_requested.set()
        app._disable_drive_worker = Mock()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            report = run_repeatability(app, dict(cycles=2, span_inc=10, speed_inc_s=5,
                profile={}, axis="A", encoder="1MU", encoder_verified=False, output_dir=folder))
        app._disable_drive_worker.assert_not_called()
        self.assertEqual(report["status"], "STOPPED")
        self.assertIn("not confirmed", report["shutdown_verification"])
        self.assertNotIn("final_statusword", report)

    def test_disabling_default_limit_cannot_bypass_mechanical_reference(self):
        app = make_app()
        app.default_limit_enabled = False
        with self.assertRaisesRegex(ValueError, "行程|置零"):
            app._validate_target(100000, {"enabled": False})

    def test_custom_limits_reject_quantized_target_outside_interval(self):
        app = make_app()
        app.default_limit_enabled = False
        app.travel_range.confirm(0, 50, "Actuator A")
        # Requested 100.8 is inside, but its actual command is 101 counts.
        with self.assertRaises(ValueError):
            app._validate_target(100.8, {"enabled": True, "min": 0, "max": 100.8})

    def test_invalid_pair_tolerance_does_not_reuse_old_value(self):
        app = linked_app()
        app.pair_tolerance_um = 20
        for value in ("", "-", "nan", "0", "-1", "inf"):
            app.pair_tolerance_var = var(value)
            app._sync_pair_tolerance()  # Editing must not throw a GUI callback error.
            with self.assertRaises(ValueError):
                app._linked_snapshot()
        app.pair_tolerance_var = var("12.5")
        app._sync_pair_tolerance()
        self.assertEqual(app._linked_snapshot()["tolerance_um"], 12.5)

    def test_close_during_connection_waits_for_worker(self):
        app = make_app()
        app.close_in_progress = False
        app.nodes = {}
        app.command_busy = True
        app._cancel_shortcut_queue = Mock()
        app.finish_exit = Mock()
        app._on_close()
        app.finish_exit.assert_not_called()
        app.schedule.assert_called_with(100, app._finish_close)

    def test_close_and_disconnect_block_new_user_commands(self):
        for flag in ("close_in_progress", "disconnect_in_progress"):
            app = make_app()
            setattr(app, flag, True)
            app.busy_var = Mock()
            with patch("xeryon_control.application.commands.threading.Thread") as thread:
                with self.assertRaises(RuntimeError):
                    app._run_command("Move absolute", Mock())
                thread.assert_not_called()

    def test_poll_does_not_compete_with_pending_close(self):
        app = make_app()
        app.node = NS(id=33)
        app.status_busy = False
        app.close_in_progress = True
        with patch("xeryon_control.application.feedback.threading.Thread") as thread:
            app._poll_status()
            thread.assert_not_called()
        app.schedule.assert_called_once()

    def test_single_axis_stop_releases_io_without_duplicate_cleanup(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app._validate_target = Mock()
        app._prepare_profile_position = Mock()
        app._command_profile_position = Mock()
        app._read_actual_position_inc = Mock(return_value=100)
        app._write_controlword = Mock()
        app._disable_drive_worker = Mock()
        def status_read(*args, **kwargs):
            app.stop_pending = True
            app.test_stop_requested.set()
            return 0x27
        app._read_statusword = Mock(side_effect=status_read)
        result = app._run_test_move(app.node, "Actuator A", 1, 5, 10,
                                   {"max_velocity": 100, "limits": {"enabled": False}}, 1)
        self.assertEqual(result["status"], "Stopped")
        app._disable_drive_worker.assert_not_called()
        app._write_controlword.assert_not_called()
        self.assertEqual(app._read_actual_position_inc.call_count, 1)

    def test_workflow_cancel_does_not_repeat_stop_verification(self):
        app = make_app()
        app.stop_pending = True
        app.test_stop_requested.set()
        app._run_standard_cycle_worker_impl = Mock(side_effect=RuntimeError("Stopped"))
        app._disable_drive_worker = Mock()
        with self.assertRaises(RuntimeError):
            app._run_standard_cycle_worker({}, False)
        app._disable_drive_worker.assert_not_called()

    def test_homing_cancel_yields_cleanup_to_stop_worker(self):
        app = make_app()
        app.node = NS(id=33)
        app._require_node = Mock(return_value=app.node)
        app._invalidate_linked_zero = Mock()
        app._disable_drive_worker = Mock()
        app.stop_pending = True
        app.test_stop_requested.set()
        with self.assertRaises(RuntimeError):
            app._home_worker(True, 0, 10)
        app._disable_drive_worker.assert_not_called()

    def test_test_settle_wait_is_stop_interruptible(self):
        app = make_app()
        app.nodes = {"Actuator A": NS(id=33)}
        app.node = app.nodes["Actuator A"]
        app._linked_snapshot = Mock(return_value={"enabled": False})
        app._invalidate_linked_zero = Mock()
        app._run_test_move = Mock(return_value={"status": "OK"})
        app.test_stop_requested.wait = Mock(return_value=True)
        with patch("xeryon_control.application.session.time.sleep") as sleep:
            app._run_actuator_test_worker({"speeds": [5], "distances": [10],
                "settle_time": 60, "step_timeout": 10, "return_after": True, "profile": {}})
        sleep.assert_not_called()
        app._run_test_move.assert_called_once()


class ControllerUiEventTests(unittest.TestCase):
    def setUp(self):
        self.window = create_console()
        self.app = self.window.session
        self.app._show_error = Mock()

    def tearDown(self):
        self.window.finish_exit()
        self.window.deleteLater()

    def test_bad_ui_event_does_not_kill_queue_pump(self):
        self.app.command_busy = True
        self.app._post("status", {"axis": "Actuator A", "position_um": "bad"})
        self.app._post("busy", "Idle")
        with patch.object(self.app, "schedule") as after:
            self.app._drain_ui_queue()
            after.assert_called_once()
            # The error window/log may consume this tick's 10 ms budget.
            if self.app.command_busy:
                self.app._drain_ui_queue()
        self.assertFalse(self.app.command_busy)
        self.app._show_error.assert_called_once()

    def test_queue_backlog_yields_to_stop_button(self):
        for index in range(1000):
            self.app._post("target", str(index))
        with patch.object(self.app, "schedule") as after:
            self.app._drain_ui_queue()
        self.assertGreater(self.app.ui_queue.qsize(), 0)
        self.assertLess(self.app.ui_queue.qsize(), 1000)
        after.assert_called_once()

    def test_visible_log_has_bounded_history(self):
        self.app._log("line\n" * 3000)
        self.assertLessEqual(self.window.advanced.log.blockCount(), 2001)

    def test_log_batch_preserves_order_around_other_events(self):
        self.app._post("log", "before event")
        self.app._post("busy", "Idle")
        self.app._post("log", "after event")
        observed = []
        handler = self.app._handle_ui_event
        def capture(kind, payload):
            if kind == "busy":
                observed.append(self.window.advanced.log.toPlainText())
            handler(kind, payload)
        with patch.object(self.app, "_handle_ui_event", side_effect=capture), patch.object(self.app, "schedule"):
            while not self.app.ui_queue.empty():
                self.app._drain_ui_queue()
        self.assertIn("before event", observed[0])
        self.assertNotIn("after event", observed[0])
        self.assertIn("after event", self.window.advanced.log.toPlainText())

    def test_axis_switch_clears_previous_axis_position_reference(self):
        self.app.last_position_inc = 30000
        self.app.last_statusword = 0x27
        self.app.active_axis_var.set("Actuator B")
        self.assertIsNone(self.app.last_position_inc)
        self.assertIsNone(self.app.last_statusword)


if __name__ == "__main__":
    unittest.main()
