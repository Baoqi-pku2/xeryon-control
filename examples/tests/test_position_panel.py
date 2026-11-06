"""Real Tk widgets, synthetic feedback and mocked I/O. Never connects to CAN."""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_gui import XeryonControlGui
from position_panel import SPEEDS


class PositionPanelTests(unittest.TestCase):
    def setUp(self):
        self.app = XeryonControlGui()
        self.app.withdraw()
        self.app.update_idletasks()
        self.panel = self.app.position_panel
        self.app._show_error = Mock()
        self.node = SimpleNamespace(id=32)
        self.app.node = self.node
        self.app.nodes = {"Actuator A": self.node}
        self.app.encoder_verified_var.set(True)
        self.app.default_limit_var.set(False)
        self.app.travel_range.confirm(0, 1000, "Actuator A")
        self.app._safe_start = Mock()
        self.feedback(100)

    def tearDown(self):
        for timer in self.app.tk.call("after", "info"):
            self.app.after_cancel(timer)
        self.app.destroy()

    def feedback(self, value, **extra):
        status = dict(position_um=value, sample_time=time.monotonic(), axis="Actuator A",
                      statusword=0x27, state="OPERATION ENABLED", mode="PROFILE_POSITION")
        status.update(extra)
        self.app._set_status(status)

    def test_drag_only_edits_target_actual_bar_waits_for_feedback(self):
        before = self.panel.canvas.coords("actual")
        body_before = self.panel.canvas.coords("body")
        rod_before = self.panel.canvas.coords("rod")
        self.panel.drag_target(SimpleNamespace(y=40))
        self.assertNotEqual(float(self.panel.target.get()), 100)
        self.assertEqual(self.panel.canvas.coords("actual"), before)
        self.app._safe_start.assert_not_called()
        self.feedback(200)
        self.assertNotEqual(self.panel.canvas.coords("actual"), before)
        self.assertGreater(self.panel.canvas.coords("actual")[1], before[1])
        self.assertEqual(self.panel.canvas.coords("body"), body_before)
        self.assertNotEqual(self.panel.canvas.coords("rod"), rod_before)

    def test_slider_entry_and_keyboard_stay_synchronized(self):
        self.panel.scale.set(220)
        self.assertEqual(float(self.panel.target.get()), 220)
        self.panel.target.set("250")
        self.assertEqual(self.panel.scale.get(), 250)
        self.panel.nudge(1)
        self.assertAlmostEqual(float(self.panel.target.get()), 250 + self.app._inc_to_um(1), places=2)

    def test_all_seven_speeds_reach_profile_and_start_uses_snapshot(self):
        for speed in SPEEDS:
            self.panel.speed.set(str(speed))
            self.panel.target.set("230")
            self.panel.start_button.invoke()
            self.app._show_error.assert_not_called()
            operation = self.app._safe_start.call_args.args[1]
            self.panel.target.set("999")
            with patch.object(self.app, "_move_absolute_worker") as move:
                operation()
                target, profile, _ = move.call_args.args
                self.assertEqual(target, 230)
                self.assertEqual(profile["velocity"], speed)
                self.assertEqual(profile["velocity_inc"], self.app._um_to_inc(speed))

    def test_expired_feedback_hides_actual_and_blocks_start(self):
        self.feedback(100, sample_time=time.monotonic()-3)
        self.assertFalse(self.panel.is_fresh())
        self.assertFalse(self.panel.canvas.find_withtag("actual"))
        self.assertTrue(self.panel.canvas.find_withtag("actual_stale"))
        self.assertEqual(self.panel.actual_text.get(), "100.00")
        self.assertIn("已过期", self.panel.actual_caption.get())
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.panel.start()
        self.app._safe_start.assert_not_called()

    def test_custom_console_speed_reaches_profile_with_limits(self):
        self.app.max_velocity_var.set('500')
        for speed in (2, 123.5, 499, 500):
            self.panel.speed.set(str(speed))
            self.panel.target.set('230')
            self.panel.start_button.invoke()
            self.app._show_error.assert_not_called()
            operation=self.app._safe_start.call_args.args[1]
            with patch.object(self.app,'_move_absolute_worker') as move:
                operation()
                self.assertEqual(move.call_args.args[1]['velocity'],speed)

    def test_custom_speed_rejects_out_of_range_and_maximum_excess(self):
        for speed in ('1.9','501','nan','inf','bad'):
            self.app._safe_start.reset_mock()
            self.panel.speed.set(speed)
            self.panel.start_button.invoke()
            self.app._safe_start.assert_not_called()
        self.panel.speed.set('500')
        self.app.max_velocity_var.set('300')
        self.panel.start_button.invoke()
        self.app._safe_start.assert_not_called()

    def test_different_axis_feedback_is_ignored(self):
        self.feedback(800, axis="Actuator B")
        self.assertEqual(self.panel.position, 100)
        self.app.active_axis_var.set("Actuator B")
        self.assertFalse(self.panel.is_fresh())

    def test_transient_failure_does_not_overwrite_pending_target(self):
        self.panel.target.set("300")
        self.app._set_status(None)
        self.feedback(105)
        self.assertEqual(self.panel.target.get(), "300")

    def test_invalid_targets_limits_encoder_and_linked_mode_block_start(self):
        for target in ("nan", "inf", "bad"):
            self.panel.target.set(target)
            self.panel.start()
            self.app._safe_start.assert_not_called()
        self.panel.target.set("150")
        self.app.limit_enabled_var.set(True)
        self.app.max_limit_var.set("120")
        self.panel.start()
        self.app._safe_start.assert_not_called()
        self.app.limit_enabled_var.set(False)
        self.app.encoder_verified_var.set(False)
        self.panel.start()
        self.app._safe_start.assert_not_called()
        self.app.encoder_verified_var.set(True)
        self.app.linked_mode_var.set(True)
        self.panel.start()
        self.app._safe_start.assert_not_called()

    def test_configured_velocity_cap_is_preserved(self):
        self.app.max_velocity_var.set("100")
        self.panel.speed.set("300")
        self.panel.start()
        self.app._safe_start.assert_not_called()

    def test_active_motion_locks_target_and_rejects_duplicate_start(self):
        self.app.active_target_inc = 500
        self.panel.redraw()
        self.assertTrue(self.panel.entry.instate(["disabled"]))
        self.panel.slide_target(300)
        self.assertEqual(float(self.panel.target.get()), 100)
        self.panel.start()
        self.app._safe_start.assert_not_called()

    def test_stop_before_worker_begins_cancels_start(self):
        self.panel.start()
        operation = self.app._safe_start.call_args.args[1]
        with patch.object(self.app, "_run_stop_command"), patch.object(self.app, "_move_absolute_worker") as move:
            self.app.stop_actuator_test()
            operation()
            move.assert_not_called()

    def test_stop_during_prepare_never_sends_target(self):
        with patch.object(self.app, "_prepare_profile_position", side_effect=lambda *args: self.app.test_stop_requested.set()), \
             patch.object(self.app, "_read_actual_position_inc", return_value=100), \
             patch.object(self.app, "_disable_drive_worker") as disable, \
             patch.object(self.app, "_command_profile_position") as command:
            self.app._move_absolute_worker(120, self.app._profile_snapshot(), self.app._limits_snapshot())
            disable.assert_called_once()
            command.assert_not_called()

    def test_zoom_preserves_target_and_does_not_change_limits(self):
        self.panel.target.set("800")
        limits = self.app._limits_snapshot()
        self.panel.span.set("100")
        self.panel.center_view()
        self.assertEqual(self.panel.target.get(), "800")
        self.assertEqual(self.app._limits_snapshot(), limits)
        self.assertEqual((self.panel.view_min, self.panel.view_max), (50, 150))

    def test_connected_user_can_confirm_label_without_changing_encoder_model(self):
        self.app.encoder_verified_var.set(False)
        self.app._post("connected", True)
        self.app._drain_ui_queue()
        self.assertTrue(self.app.encoder_combo.instate(["disabled"]))
        self.assertFalse(self.app.encoder_check.instate(["disabled"]))
        self.assertFalse(self.panel.encoder_check.instate(["disabled"]))
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.panel.encoder_check.invoke()
        self.assertTrue(self.app.encoder_verified_var.get())
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.panel.start_button.invoke()
        self.app._safe_start.assert_called_once()

    def test_cancel_label_confirmation_disables_start_again(self):
        self.panel.encoder_check.invoke()
        self.assertFalse(self.app.encoder_verified_var.get())
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.panel.start_button.invoke()
        self.app._safe_start.assert_not_called()

    def test_full_stroke_and_zoom_do_not_change_slider_or_limits(self):
        self.panel.show_full_travel()
        self.assertEqual((self.panel.view_min, self.panel.view_max), (-30000, 30000))
        before = (self.panel.scale.cget("from"), self.panel.scale.cget("to"))
        self.panel.span.set("100")
        self.panel.center_view()
        self.assertEqual((self.panel.scale.cget("from"), self.panel.scale.cget("to")), before)
        self.assertEqual(self.app.travel_range.command_bounds, (-29000, 29000))

    def test_mapping_required_and_cannot_target_reserved_end_zone(self):
        self.panel.target.set("30000")
        self.panel.start()
        self.app._safe_start.assert_not_called()
        self.app.travel_range.invalidate()
        self.panel.target.set("200")
        self.panel.redraw()
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.assertTrue(self.panel.canvas.find_withtag("actual"))
        self.assertTrue(self.panel.canvas.find_withtag("target"))

    def test_unmapped_positions_are_visible_and_preview_does_not_move(self):
        self.app.travel_range.invalidate()
        self.feedback(-24975.76)
        self.panel.show_full_travel()
        self.assertTrue(self.panel.canvas.find_withtag("actual"))
        self.assertTrue(self.panel.canvas.find_withtag("target"))
        self.assertEqual(self.panel.actual_text.get(), "-24,975.76")
        self.panel.slide_target(-24000)
        self.assertEqual(self.app.target_position_var.get(), "-24000.00")
        self.panel.span.set("1000")
        self.panel.center_view()
        self.assertTrue(self.panel.canvas.find_withtag("actual"))
        self.assertTrue(self.panel.canvas.find_withtag("target"))
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.app._safe_start.assert_not_called()

    def test_first_feedback_does_not_fake_physical_midpoint(self):
        self.panel.invalidate()
        self.panel.encoder_view_center = 0.0
        self.feedback(-24975.762)
        self.panel.show_full_travel()
        self.assertEqual(self.panel.encoder_view_center, 0.0)
        self.assertEqual(self.panel.display_bounds(), (-30000, 30000))
        top, bottom = self.panel.plot_vertical_bounds()
        expected_y = top + (-24975.762 + 30000) / 60000 * (bottom - top)
        actual = self.panel.canvas.coords("actual")
        self.assertAlmostEqual((actual[1] + actual[3]) / 2, expected_y)
        self.assertLess((actual[1] + actual[3]) / 2, (top + bottom) / 2)

    def test_transient_loss_preserves_reading_and_target_with_stale_label(self):
        self.panel.target.set("300")
        self.app._set_status(None)
        self.assertEqual(self.panel.actual_text.get(), "100.00")
        self.assertIn("已过期",self.panel.actual_caption.get())
        self.assertTrue(self.panel.canvas.find_withtag("actual_stale"))
        self.assertTrue(self.panel.canvas.find_withtag("target"))
        self.assertEqual(self.panel.target.get(),"300")
        self.feedback(105)
        self.assertTrue(self.panel.canvas.find_withtag("actual"))
        self.assertFalse(self.panel.canvas.find_withtag("actual_stale"))

    def test_main_advanced_and_commanded_target_share_coordinates(self):
        self.app.target_position_var.set("250")
        self.assertEqual(self.panel.target.get(),"250")
        self.panel.target.set("350")
        self.assertEqual(self.app.target_position_var.get(),"350")
        self.app._post("target_coordinate", -25001.9674)
        self.app._drain_ui_queue()
        self.assertEqual(self.panel.target.get(),"-25001.97")
        self.assertTrue(self.panel.canvas.find_withtag("target"))
        self.app._safe_start.assert_not_called()

    def test_long_status_keeps_panels_accessible_in_compact_console(self):
        self.app.geometry("1100x760+4000+100")
        self.app.deiconify()
        self.app.notebook.select(0)
        self.app.default_limit_var.set(True)
        self.panel.apply_user_zero(100, time.monotonic())
        self.feedback(100, state="OPERATION ENABLED")
        self.app.target_status_var.set("1000.00 um")
        self.app.shortcut_status_var.set("未排队 · 连续点击可按顺序执行")
        def settle():
            for _ in range(3):
                self.app.update()
                time.sleep(0.01)
        settle()
        visual = self.panel.visual_card
        controls = self.panel.control_card
        shortcuts = self.panel.shortcut_card
        baseline = (visual.winfo_width(), controls.winfo_width())
        self.assertEqual(self.app.winfo_width(), 1100)
        self.assertEqual(self.app.winfo_height(), 760)
        self.feedback(7153.07, statusword=0x40, state="SWITCH ON DISABLED", mode="0")
        self.app.target_position_var.set("12000.06")
        self.app.target_status_var.set("已停止 · 已核对失能与位置稳定")
        self.app.shortcut_status_var.set("快捷流程中止 · Node 33: Failed to confirm control mode PROFILE_POSITION")
        for speed in (5, 20, 50, 300):
            self.panel.speed.set(str(speed))
        self.panel.span.set("1000")
        self.panel.center_view()
        self.panel.show_full_travel()
        self.app.default_limit_var.set(False)
        self.panel.default_limit_changed()
        settle()
        current = (visual.winfo_width(), controls.winfo_width())
        self.assertEqual(current, baseline)
        # Both columns and the complete preset section fit without page scrolling.
        self.assertFalse(self.panel._compact)
        self.assertEqual(visual.winfo_y(), controls.winfo_y())
        self.assertGreaterEqual(shortcuts.winfo_y(), visual.winfo_y() + visual.winfo_height())
        footer = controls.content.winfo_children()[-1]
        self.assertLessEqual(footer.winfo_y() + footer.winfo_height(), controls.content.winfo_height())
        self.assertLessEqual(shortcuts.winfo_x() + shortcuts.winfo_width(), self.panel.winfo_width())
        self.assertLessEqual(shortcuts.winfo_y() + shortcuts.winfo_height(), self.panel.winfo_height())

    def test_console_has_one_advanced_entry_and_four_speed_presets(self):
        self.assertEqual(self.app.notebook.index("end"), 2)
        self.assertEqual(self.app.notebook.tab(0, "text"), "控制台")
        self.assertEqual(self.app.notebook.tab(1, "text"), "高级设置")
        self.assertEqual(len(self.panel.speed_buttons), 4)
        self.assertEqual([button.cget("value") for button in self.panel.speed_buttons], ["5", "20", "50", "300"])

    def test_motion_setup_uses_current_position_without_switching_pages(self):
        self.app.travel_range.invalidate()
        self.panel.open_motion_setup()
        self.assertIsNone(self.app.grab_current())
        self.panel.use_current_position_as_center()
        self.assertEqual(float(self.panel.center_coordinate.get()), 100)
        self.assertTrue(self.panel.center_checked.get())
        self.panel.apply_motion_setup()
        self.assertTrue(self.app.travel_range.confirmed)
        self.assertEqual(self.app.travel_range.center_um, 100)
        self.assertFalse(self.panel.setup_window.winfo_exists())
        self.assertEqual(self.app.notebook.tab(self.app.notebook.select(), "text"), "控制台")
        self.app._safe_start.assert_not_called()

    def test_applying_mapping_does_not_move_or_rezero_device(self):
        self.panel.center_coordinate.set("50")
        self.panel.end_margin.set("2000")
        self.assertFalse(self.app.travel_range.confirmed)
        self.panel.center_checked.set(True)
        self.panel.apply_travel()
        self.assertTrue(self.app.travel_range.confirmed)
        self.assertEqual(self.app.travel_range.mechanical_bounds, (-29950, 30050))
        self.assertEqual(self.app.travel_range.command_bounds, (-27950, 28050))
        self.assertEqual(self.app._limits_snapshot(), {"enabled":False,"min":-27950,"max":28050})
        self.assertEqual(self.panel.position, 100)
        self.app._safe_start.assert_not_called()
        self.panel.invalidate()
        self.assertFalse(self.app.travel_range.confirmed)

    def test_work_limits_cannot_expand_confirmed_stroke(self):
        self.app.limit_enabled_var.set(True)
        self.app.max_limit_var.set("50000")
        with self.assertRaises(ValueError):
            self.app._limits_snapshot()
        self.app.max_limit_var.set("20000")
        self.assertEqual(self.app._limits_snapshot()["max"], 20000)

    def test_stop_failure_blocks_start_despite_fresh_position(self):
        self.app.stop_unconfirmed = True
        self.panel.redraw()
        self.assertTrue(self.panel.start_button.instate(["disabled"]))
        self.assertIn("停止未确认",self.panel.readiness_text.get())
        self.panel.start_button.invoke()
        self.app._safe_start.assert_not_called()

    def test_error_notice_does_not_grab_stop_input(self):
        XeryonControlGui._show_error(self.app,"test no reply")
        self.assertIsNone(self.app.grab_current())
        self.assertTrue(self.app.error_window.winfo_exists())

    def test_user_zero_changes_display_without_changing_device_frame(self):
        self.panel.apply_user_zero(100, time.monotonic())
        self.assertEqual(self.panel.actual_text.get(), "0.00")
        self.assertEqual(self.app.user_position_var.get(), "0.00")
        self.assertEqual(self.panel.user_target.get(), "0.00")
        self.assertEqual(float(self.app.target_position_var.get()), 100)
        self.assertEqual(self.app.travel_range.center_um, 30100)
        self.assertEqual(self.app.travel_range.command_bounds, (150, 60050))
        self.assertEqual(self.app._limits_snapshot(), {"enabled": False, "min": 150, "max": 60050})
        self.assertTrue(self.app.travel_range.confirmed)
        self.app._safe_start.assert_not_called()
        self.feedback(110)
        self.assertEqual(self.panel.actual_text.get(), "10.00")
        self.panel.user_target.set("2500")
        self.assertEqual(float(self.panel.target.get()), 2600)
        self.panel.start()
        operation = self.app._safe_start.call_args.args[1]
        with patch.object(self.app,"_move_absolute_worker") as move:
            operation()
        self.assertEqual(move.call_args.args[0],2600)

    def test_zero_preserves_mechanical_limits_and_disconnect_clears_offset(self):
        self.panel.apply_user_zero(100, time.monotonic())
        self.panel.user_target.set("60000")  # raw 60100 exceeds the 59000 command limit
        self.panel.start()
        self.app._safe_start.assert_not_called()
        self.panel.invalidate()
        self.assertFalse(self.panel.user_zero_set)
        self.assertEqual(self.panel.user_zero_um,0)

    def test_zero_capture_reads_only_and_rejects_moving_drive(self):
        with patch.object(self.app,"_read_statusword",return_value=0x250), \
             patch.object(self.app,"_read_actual_position_inc",side_effect=[-24780,-24780]), \
             patch.object(self.app,"_write_controlword") as write, \
             patch.object(self.app,"_post") as post:
            self.app._capture_user_zero_worker(self.node,"Actuator A")
            write.assert_not_called()
            self.assertEqual(post.call_args.args[0],"user_zero")
            self.assertAlmostEqual(post.call_args.args[1]["raw_um"],-24975.762)
        with patch.object(self.app,"_read_statusword",return_value=0x237):
            with self.assertRaises(ValueError):
                self.app._capture_user_zero_worker(self.node,"Actuator A")

    def test_endpoint_zero_places_body_at_top_and_derives_range(self):
        self.app.default_limit_var.set(True)
        self.app.travel_range.invalidate()
        self.panel.apply_user_zero(100, time.monotonic())
        self.assertEqual(self.panel.display_bounds(), (100, 15100))
        self.app.default_limit_var.set(False)
        self.panel.show_full_travel()
        self.assertEqual(self.panel.display_bounds(), (100, 60100))
        texts = [self.panel.canvas.itemcget(item, "text") for item in self.panel.canvas.find_all()
                 if self.panel.canvas.type(item) == "text"]
        self.assertFalse(any("超出视图" in text for text in texts))
        self.assertNotIn("-0.000", self.panel.end_distance_text.get())
        actual = self.panel.canvas.coords("actual")
        top, bottom = self.panel.plot_vertical_bounds()
        self.assertEqual((actual[1]+actual[3])/2, top)
        rod = self.panel.canvas.coords("rod")
        body = self.panel.canvas.coords("body")
        self.assertEqual(rod[3], top)
        self.assertEqual((body[1]+body[3])/2, top)
        before = actual[:]
        self.panel.drag_target(SimpleNamespace(y=bottom))
        high_inc = self.app.travel_range.count_bounds(self.app._inc_to_um(1))[1]
        self.assertAlmostEqual(float(self.panel.user_target.get()), self.app._inc_to_um(high_inc) - 100, places=2)
        self.assertEqual(self.panel.canvas.coords("actual"), before)
        self.assertTrue(self.app.travel_range.confirmed)
        self.assertFalse(self.panel.start_button.instate(["disabled"]))
        self.app._safe_start.assert_not_called()

    def test_default_limit_is_selected_and_enforced_before_motion(self):
        self.app.default_limit_var.set(True)
        self.assertTrue(self.app.default_limit_var.get())
        self.app.travel_range.invalidate()
        self.panel.apply_user_zero(100, time.monotonic())
        self.assertEqual(self.panel.display_bounds(), (100, 15100))
        self.app._validate_target(13100, {"enabled": False, "min": 150, "max": 60050})
        for target in (13100.4, 14000, 20000):
            with self.assertRaisesRegex(ValueError, "默认限位"):
                self.app._validate_target(target, {"enabled": False, "min": 150, "max": 60050})
        self.app.default_limit_var.set(False)
        self.app._validate_target(20000, {"enabled": False, "min": 150, "max": 60050})
        self.panel.show_full_travel()
        self.assertEqual(self.panel.display_bounds(), (100, 60100))

    def test_default_limit_toggle_clamps_target_and_restores_full_view(self):
        self.app.travel_range.invalidate()
        self.panel.apply_user_zero(100, time.monotonic())
        self.app.default_limit_var.set(False)
        self.panel.user_target.set("14000")
        self.app.default_limit_var.set(True)
        self.panel.default_limit_changed()
        self.assertLessEqual(float(self.panel.user_target.get()), 13000)
        self.assertEqual(self.panel.display_bounds(), (100, 15100))
        self.panel.span.set("60000")
        self.panel.center_view()
        self.assertEqual((self.panel.view_min, self.panel.view_max), (100, 15100))
        self.app.default_limit_var.set(False)
        self.panel.default_limit_changed()
        self.assertEqual(self.panel.display_bounds(), (100, 60100))
        self.app._safe_start.assert_not_called()

    def test_photo_view_can_zoom_negative_feedback_without_clipping_or_rezeroing(self):
        self.app.travel_range.invalidate()
        self.panel.apply_user_zero(100, time.monotonic())
        self.app.travel_range.invalidate()
        self.feedback(50)
        self.panel.span.set("1000")
        self.panel.center_view()
        self.assertEqual((self.panel.view_min, self.panel.view_max), (-450, 550))
        self.assertEqual(self.panel.actual_text.get(), "-50.00")
        self.assertEqual(self.panel.user_zero_um, 100)
        top, bottom = self.panel.plot_vertical_bounds()
        self.panel.drag_target(SimpleNamespace(y=(top+bottom)/2))
        self.assertEqual(float(self.panel.target.get()), 50)
        self.assertEqual(float(self.panel.user_target.get()), -50)
        self.assertFalse(self.app.travel_range.confirmed)
        self.app._safe_start.assert_not_called()
    def test_axis_switch_preserves_each_axis_mechanical_mapping(self):
        from travel_range import TravelRange
        travel_a = TravelRange()
        travel_a.confirm(30000, 50, "Actuator A")
        travel_b = TravelRange()
        travel_b.confirm(40000, 50, "Actuator B")
        self.app.travel_ranges = {"Actuator A": travel_a, "Actuator B": travel_b}
        self.app.active_axis_var.set("Actuator B")
        self.assertEqual(self.app.travel_ranges, {"Actuator A": travel_a, "Actuator B": travel_b})
        self.assertIs(self.app.travel_range, travel_b)


if __name__ == "__main__":
    unittest.main()
