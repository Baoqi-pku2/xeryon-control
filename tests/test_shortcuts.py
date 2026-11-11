"""Offline regression checks for preset motion queues; never connects to CAN."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xeryon_control.ui.main_window import XeryonControlGui
from xeryon_control.domain.shortcut_presets import SHORTCUT_PRESETS


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.app = XeryonControlGui()
        self.app.withdraw()
        self.app.update_idletasks()
        self.node = SimpleNamespace(id=33)
        self.app.node = self.node
        self.app.nodes = {"Actuator A": self.node}
        self.app.shortcut_dwell_var.set("0")  # Legacy queue tests exercise immediate continuation.
        self.app.position_panel.apply_user_zero(100, time.monotonic())
        self.app._safe_start = Mock()
        self.app._show_error = Mock()

    def tearDown(self):
        for timer in self.app.tk.call("after", "info"):
            self.app.after_cancel(timer)
        self.app.destroy()

    def complete_active(self):
        generation = self.app.shortcut_active["generation"]
        self.app._post("shortcut_done", generation)
        self.app._drain_ui_queue()

    def test_requested_presets_and_buttons(self):
        self.assertEqual(
            SHORTCUT_PRESETS,
            (
                (1, "到达GT上", 5000.0, 300.0),
                (2, "到达dura上", 7000.0, 20.0),
                (3, "进入脑区", 12000.0, 5.0),
                (4, "撤出脑区", 7000.0, 5.0),
                (5, "撤到工作起点", 50.0, 300.0),
                (6, "清洗下电极", 8000.0, 50.0),
                (7, "清洗撤电极", 50.0, 300.0),
            ),
        )
        self.assertEqual(list(self.app.position_panel.shortcut_buttons), list(range(1, 8)))

    def begin_stale_dwell(self):
        for number in (1, 2):
            self.app.position_panel.shortcut_buttons[number].invoke()
        self.app.position_panel.start()
        generation = self.app.shortcut_generation
        self.app.position_panel.received_at = time.monotonic() - 10
        self.app._handle_shortcut_done(generation)
        return generation

    def test_stale_dwell_waits_without_motion_then_resumes_with_fresh_feedback(self):
        generation = self.begin_stale_dwell()
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.assertEqual(self.app.shortcut_active['number'], 1)
        self.app._show_error.assert_not_called()
        self.app.position_panel.received_at = time.monotonic()
        self.app._continue_shortcut_after_dwell(generation)
        self.assertEqual(self.app._safe_start.call_count, 2)
        self.assertEqual(self.app.shortcut_active['number'], 2)

    def test_persistent_stale_feedback_cancels_without_next_motion(self):
        generation = self.begin_stale_dwell()
        self.app.shortcut_feedback_wait_started = time.monotonic() - 2.1
        self.app._continue_shortcut_after_dwell(generation)
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_pending, [])
        self.app._show_error.assert_called_once()

    def test_stop_while_waiting_feedback_prevents_auto_resume(self):
        generation = self.begin_stale_dwell()
        self.app.test_stop_requested.set()
        self.app.position_panel.received_at = time.monotonic()
        self.app._continue_shortcut_after_dwell(generation)
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_pending, [])

    def test_clicks_only_toggle_selection_and_color_without_motion(self):
        for number in (1, 2, 3):
            self.app.position_panel.shortcut_buttons[number].invoke()
        self.app._show_error.assert_not_called()
        self.app._safe_start.assert_not_called()

        self.assertEqual(self.app.shortcut_selected, [1, 2, 3])
        self.assertEqual(self.app.position_panel.user_target.get(), "0.00")
        self.assertEqual(self.app.position_panel.speed.get(), "5")
        for number in (1, 2, 3):
            self.assertEqual(
                self.app.position_panel.shortcut_buttons[number].cget("style"),
                "ShortcutSelected.TButton")
        self.app.position_panel.shortcut_buttons[2].invoke()
        self.assertEqual(self.app.shortcut_selected, [1, 3])
        self.assertEqual(self.app.position_panel.shortcut_buttons[2].cget("style"), "Shortcut.TButton")
        self.app._safe_start.assert_not_called()

    def test_custom_sequence_speed_is_validated_against_maximum(self):
        self.app.max_velocity_var.set('500')
        with patch('xeryon_control.ui.main_window.SHORTCUT_PRESETS', ((1,'随机步骤',500,423),)):
            step=self.app._shortcut_snapshot(1)
            self.assertEqual(step['profile']['velocity'],423)
            self.app.max_velocity_var.set('300')
            with self.assertRaises(ValueError):
                self.app._shortcut_snapshot(1)

    def test_selection_is_available_before_connection(self):
        self.app.node = None
        self.app.nodes = {}
        self.app.position_panel.redraw()
        self.assertTrue(self.app.position_panel.shortcut_buttons[1].instate(["!disabled"]))
        self.app.position_panel.shortcut_buttons[1].invoke()
        self.assertEqual(self.app.shortcut_selected, [1])
        self.app._safe_start.assert_not_called()
        self.app._show_error.assert_not_called()

    def test_one_two_three_start_only_from_main_button_and_run_in_order(self):
        for number in (1, 2, 3):
            self.app.position_panel.shortcut_buttons[number].invoke()
        self.app._safe_start.assert_not_called()
        self.app.position_panel.start()
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.assertEqual(self.app.shortcut_active["number"], 1)
        self.assertEqual([step["number"] for step in self.app.shortcut_pending], [2, 3])
        self.assertEqual(self.app.position_panel.user_target.get(), "5000.00")
        self.assertEqual(self.app.position_panel.speed.get(), "300")
        self.assertEqual(self.app.velocity_var.get(), "300")
        self.assertEqual(self.app.position_panel.shortcut_buttons[1].cget("style"), "ShortcutActive.TButton")
        label, operation = self.app._safe_start.call_args.args
        self.assertIn("快捷 1", label)
        with patch.object(self.app, "_move_absolute_worker") as move:
            operation()
        target, profile, _ = move.call_args.args
        self.assertEqual(target, 5100)
        self.assertEqual(profile["velocity"], 300)
        self.assertEqual(profile["velocity_inc"], self.app._um_to_inc(300))

        self.complete_active()
        self.assertEqual(self.app._safe_start.call_count, 2)
        self.assertEqual(self.app.shortcut_active["number"], 2)
        self.assertEqual(self.app.position_panel.user_target.get(), "7000.00")
        self.assertEqual(self.app.position_panel.speed.get(), "20")

        self.complete_active()
        self.assertEqual(self.app._safe_start.call_count, 3)
        self.assertEqual(self.app.shortcut_active["number"], 3)
        self.assertEqual(self.app.position_panel.user_target.get(), "12000.00")
        self.assertEqual(self.app.position_panel.speed.get(), "5")

        self.complete_active()
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_selected, [])
        self.assertEqual(self.app._safe_start.call_count, 3)
        self.assertIn("全部到位", self.app.shortcut_status_var.get())

    def test_all_presets_fit_the_reported_real_encoder_mapping(self):
        self.app.position_panel.apply_user_zero(-141.11, time.monotonic())
        for number in range(1, 8):
            self.app.position_panel.shortcut_buttons[number].invoke()
        self.app.start_selected_shortcuts()
        self.app._show_error.assert_not_called()
        self.app._safe_start.assert_called_once()
        steps = [self.app.shortcut_active, *self.app.shortcut_pending]
        self.assertEqual([step["number"] for step in steps], list(range(1, 8)))
        self.assertEqual([step["target"] for step in steps],
                         [4858.89, 6858.89, 11858.89, 6858.89, -91.11000000000001, 7858.89, -91.11000000000001])

    def test_retract_presets_are_inside_new_default_margin(self):
        for number in (5, 7):
            self.app.start_shortcut(number)
            self.app._show_error.assert_not_called()
            self.assertEqual(self.app.shortcut_active["user_target"], 50)
            self.assertEqual(self.app.shortcut_active["speed"], 300)
            self.app._cancel_shortcut_queue()
        self.assertEqual(self.app.travel_range.command_bounds, (150, 60050))

    def test_clear_selection_never_starts_or_clears_active_motion(self):
        for number in (1, 2, 3):
            self.app.position_panel.shortcut_buttons[number].invoke()
        self.app.clear_shortcut_queue()
        self.assertEqual(self.app.shortcut_selected, [])
        self.app._safe_start.assert_not_called()
        self.app.start_shortcut(1)
        self.app.clear_shortcut_queue()
        self.assertEqual(self.app.shortcut_active["number"], 1)

    def test_selected_sequence_is_fully_validated_before_first_motion(self):
        self.app.position_panel.shortcut_buttons[1].invoke()
        self.app.position_panel.shortcut_buttons[2].invoke()
        self.app.limit_enabled_var.set(True)
        self.app.min_limit_var.set("150")
        self.app.max_limit_var.set("6000")
        self.app.position_panel.start()
        self.app._safe_start.assert_not_called()
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_pending, [])
        self.assertEqual(self.app.shortcut_selected, [1, 2])
        self.app._show_error.assert_called_once()

    def test_not_ready_start_click_reports_reason_instead_of_staying_silent(self):
        self.app.node = None
        self.app.nodes = {}
        self.app.position_panel.redraw()
        self.assertFalse(self.app.position_panel.start_button.instate(["disabled"]))
        self.app.position_panel.shortcut_buttons[1].invoke()
        self.app.position_panel.start_button.invoke()
        self.app._safe_start.assert_not_called()
        self.app._show_error.assert_called_once()
        self.assertIn("未启动", self.app.target_status_var.get())
        self.assertIn("收到“开始运动”点击", self.app.log_text.get("1.0", "end"))

    def test_main_start_click_has_immediate_status_and_log_feedback(self):
        self.app.position_panel.shortcut_buttons[1].invoke()
        self.app.position_panel.start()
        self.assertIn("正在检查", self.app.target_status_var.get())
        self.assertIn("收到“开始运动”点击；快捷选择：1", self.app.log_text.get("1.0", "end"))
        self.app._safe_start.assert_called_once()

    def test_stop_clears_queue_and_stale_completion_cannot_restart(self):
        self.app.start_shortcut(1)
        generation = self.app.shortcut_generation
        self.app.start_shortcut(2)
        with patch.object(self.app, "_run_stop_command"):
            self.app.stop_actuator_test()
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_pending, [])
        self.app._post("shortcut_done", generation)
        self.app._drain_ui_queue()
        self.assertEqual(self.app._safe_start.call_count, 1)

    def test_unrelated_command_is_blocked_between_shortcut_steps(self):
        self.app.start_shortcut(1)
        self.app.start_shortcut(2)
        self.complete_active()
        with self.assertRaisesRegex(RuntimeError, "快捷流程进行中"):
            self.app._run_command("Enable drive", Mock())

    def test_failed_step_cancels_all_pending_steps(self):
        self.app.start_shortcut(1)
        generation = self.app.shortcut_generation
        self.app.start_shortcut(2)
        self.app._post("shortcut_failed", {"generation": generation, "reason": "通信异常"})
        self.app._drain_ui_queue()
        self.assertIsNone(self.app.shortcut_active)
        self.assertEqual(self.app.shortcut_pending, [])
        self.assertIn("通信异常", self.app.shortcut_status_var.get())


if __name__ == "__main__":
    unittest.main()
