"""Real Qt widgets, synthetic feedback and mocked I/O. Never connects to CAN."""
import time
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
import unittest
from tests.qt_support import create_console
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xeryon_control.ui.travel_readout import TravelReadout
from tools.simulated_node import SimulatedNode


class PositionPanelTests(unittest.TestCase):
    def setUp(self):
        self.window = create_console()
        self.app = self.window.session
        self.ui = self.window.panel
        self.panel = self.app.position_panel
        self.app._show_error = Mock()
        self.node = SimpleNamespace(id=32)
        self.app.node = self.node
        self.app.nodes = {"Actuator A": self.node}
        self.app.default_limit_var.set(False)
        self.app.travel_range.confirm(0, 1000, "Actuator A")
        self.app._safe_start = Mock()
        self.feedback(100)

    def tearDown(self):
        self.window.finish_exit()
        self.window.deleteLater()

    def feedback(self, value, **extra):
        status = dict(position_um=value, sample_time=time.monotonic(), axis="Actuator A",
                      statusword=0x27, state="OPERATION ENABLED", mode="PROFILE_POSITION")
        status.update(extra)
        self.app._set_status(status)

    def test_drag_only_edits_target_actual_bar_waits_for_feedback(self):
        plot=self.ui.plot
        before=plot.y_for(self.panel.position)
        QTest.mouseClick(plot,Qt.MouseButton.LeftButton,pos=QPoint(85,60))
        self.assertNotEqual(float(self.panel.target.get()),100)
        self.assertEqual(plot.y_for(self.panel.position),before)
        self.app._safe_start.assert_not_called()
        self.feedback(200)
        self.assertGreater(plot.y_for(self.panel.position),before)

    def test_slider_entry_and_keyboard_stay_synchronized(self):
        self.ui.slider.setValue(5200)
        lo,hi=self.panel.target_bounds()
        self.assertAlmostEqual(float(self.panel.target.get()),lo+.52*(hi-lo),places=2)
        self.panel.target.set("250")
        self.assertEqual(self.ui.slider.value(),round(10000*(250-lo)/(hi-lo)))
        QTest.keyClick(self.ui.plot,Qt.Key.Key_Down)
        self.assertAlmostEqual(float(self.panel.target.get()),250+self.app._inc_to_um(1),places=2)

    @patch("time.monotonic", new=lambda: 1000.0)
    def test_speed_buttons_reach_profile_and_start_uses_snapshot(self):
        self.feedback(100)
        self.assertEqual(set(self.ui.speeds), {5, 20, 50, 300})
        for speed, button in self.ui.speeds.items():
            button.click()
            self.panel.target.set("230")
            self.ui.start.click()
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
        self.assertIsNotNone(self.panel.position)
        self.assertEqual(self.panel.actual_text.get(), "—")
        self.assertIn("上次编码器位置 100.00 µm", self.panel.encoder_caption.get())
        self.assertIn("已过期", self.panel.actual_caption.get())
        self.assertTrue(self.ui.start.isEnabled())
        self.panel.start()
        self.app._safe_start.assert_not_called()

    def test_custom_console_speed_reaches_profile_with_limits(self):
        self.app.max_velocity_var.set('500')
        for speed in (2, 123.5, 499, 500):
            self.panel.speed.set(str(speed))
            self.panel.target.set('230')
            self.ui.start.click()
            self.app._show_error.assert_not_called()
            operation=self.app._safe_start.call_args.args[1]
            with patch.object(self.app,'_move_absolute_worker') as move:
                operation()
                self.assertEqual(move.call_args.args[1]['velocity'],speed)

    def test_custom_speed_rejects_out_of_range_and_maximum_excess(self):
        for speed in ('1.9','501','nan','inf','bad'):
            self.app._safe_start.reset_mock()
            self.panel.speed.set(speed)
            self.ui.start.click()
            self.app._safe_start.assert_not_called()
        self.panel.speed.set('500')
        self.app.max_velocity_var.set('300')
        self.ui.start.click()
        self.app._safe_start.assert_not_called()

    def test_different_axis_feedback_is_ignored(self):
        self.feedback(800, axis="Actuator B")
        self.assertEqual(self.panel.position, 100)
        self.app.active_axis_var.set("Actuator B")
        self.assertFalse(self.panel.is_fresh())

    def test_invalid_targets_limits_and_linked_mode_block_start(self):
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
        self.assertTrue(not self.ui.entry.isEnabled())
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

    def _setup_shifted_start(self, shortcut=False, zero=0, initial_offset=100):
        app = self.app
        app.inc_per_mm = 1000
        self.panel.apply_user_zero(zero, time.monotonic())
        self.feedback(zero + initial_offset)
        self.panel.user_target.set("1000")
        if shortcut:
            app.toggle_shortcut(1)
            app.toggle_shortcut(2)
        app._read_actual_position_inc = Mock(side_effect=[zero + initial_offset, zero + 350])
        app._read_statusword = Mock(return_value=0x40)
        app._disable_drive_worker = Mock(return_value={32: {"position_inc": zero + 352, "statusword": 0x40}})
        self.node.sdo = {key: SimpleNamespace(raw=0) for key in (
            "Profile velocity", "Profile acceleration", "Max profile velocity", "Max acceleration",
            "Quick stop deceleration", "Following error window", "Following error time out")}
        self.node.sdo[0x60A4] = {1: SimpleNamespace(raw=0)}
        app._prepare_profile_position = lambda node, profile, reference: app._write_profile(node, profile)
        app._start_motion_trace = Mock()
        app._command_profile_position = Mock()
        self.panel.start()
        self._finish_submitted_command()
        app._command_profile_position.assert_not_called()
        return app.start_confirmation

    def _finish_submitted_command(self):
        app = self.app
        label, worker = app._safe_start.call_args.args
        app.command_busy = True
        app._command_runner(label, worker, app.cancel_generation)
        while not app.ui_queue.empty():
            app._handle_ui_event(*app.ui_queue.get_nowait())

    def test_continue_recomputes_from_verified_stop_and_keeps_absolute_target(self):
        zero = -18000
        review = self._setup_shifted_start(zero=zero)
        app = self.app
        self.assertEqual(review.stopped_inc, zero + 352)
        self.assertGreater(self.node.sdo[0x60A4][1].raw, 0)
        self.assertNotIn("jerk_inc", review.motion.profile)
        labels = [w.text() for w in self.window.confirmation.findChildren(QLabel)]
        self.assertTrue(any("新起点 +352.00" in s and "目标 +1000.00" in s for s in labels))
        self.assertTrue(any("编码器绝对位置 -17648.00 µm" in s for s in labels))
        self.assertTrue(app.test_stop_requested.is_set())
        self.assertIsNone(QApplication.activeModalWidget())
        app._read_actual_position_inc = Mock(side_effect=[zero + 353, zero + 354])
        buttons = self.window.confirmation.findChildren(QPushButton)
        self.assertEqual([b.text() for b in buttons], ["继续", "取消"])
        self.window.continue_button.click()
        self._finish_submitted_command()
        self.assertEqual(app.active_start_inc, zero + 354)
        app._command_profile_position.assert_called_once_with(self.node, zero + 1000)
        self.assertEqual(app.target_remaining_var.get(), "646.00")
        app._continue_start(review)
        self.assertEqual(app._safe_start.call_count, 2)

    def test_changed_again_needs_new_confirmation_and_cancel_clears_shortcuts(self):
        review = self._setup_shifted_start(shortcut=True)
        app = self.app
        self.assertEqual(review.motion.step["number"], 1)
        self.assertEqual([step["number"] for step in app.shortcut_pending], [2])
        app._read_actual_position_inc = Mock(return_value=600)
        app._disable_drive_worker.return_value[32]["position_inc"] = 600
        app._continue_start(review)
        self._finish_submitted_command()
        self.assertEqual(app.start_confirmation.stopped_inc, 600)
        app._dismiss_start_confirmation()
        self.assertIsNone(app.shortcut_active)
        self.assertFalse(app.shortcut_pending)
        app._command_profile_position.assert_not_called()

    def test_stop_invalidates_open_confirmation(self):
        review = self._setup_shifted_start()
        with patch("xeryon_control.application.stop.threading.Thread"):
            self.app.stop_actuator_test()
        self.app._continue_start(review)
        self.assertIsNone(self.app.start_confirmation)
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.app._command_profile_position.assert_not_called()

    def test_changed_limits_cannot_resume_old_target(self):
        review = self._setup_shifted_start()
        self.app.limit_enabled_var.set(True)
        self.app.min_limit_var.set("50")
        self.app.max_limit_var.set("500")
        self.app._continue_start(review)
        self.assertIs(self.app.start_confirmation, review)
        self.assertTrue(not self.window.continue_button.isEnabled())
        self.assertEqual(self.app._safe_start.call_count, 1)
        self.app._command_profile_position.assert_not_called()
        self.app._show_error.assert_not_called()
        self.app._dismiss_start_confirmation()

    def test_editing_target_requires_approval_of_updated_relative_preview(self):
        review = self._setup_shifted_start(zero=-18000)
        self.panel.user_target.set("1200")
        self.window.continue_button.click()
        revised = self.app.start_confirmation
        self.assertIsNot(revised, review)
        self.assertEqual(revised.motion.target, -16800)
        self.app._command_profile_position.assert_not_called()
        self.assertEqual(self.app._safe_start.call_count, 1)
        labels = [w.text() for w in self.window.confirmation.findChildren(QLabel)]
        self.assertTrue(any("目标 +1200.00" in s and "预计位移 +848.00" in s for s in labels))
        self.app._read_actual_position_inc = Mock(return_value=-17648)
        self.window.continue_button.click()
        self._finish_submitted_command()
        self.app._command_profile_position.assert_called_once_with(self.node, -16800)

    def test_unverified_stop_never_offers_continue(self):
        app = self.app
        self.panel.target.set("1000")
        app._read_actual_position_inc = Mock(return_value=900)
        app._disable_drive_worker = Mock(side_effect=RuntimeError("停止未确认"))
        app._command_profile_position = Mock()
        self.panel.start()
        self._finish_submitted_command()
        self.assertIsNone(app.start_confirmation)
        app._show_error.assert_called_once_with("停止未确认")
        app._command_profile_position.assert_not_called()

    def test_stop_during_prepare_never_sends_target(self):
        with patch.object(self.app, "_prepare_profile_position", side_effect=lambda *args: self.app.test_stop_requested.set()), \
             patch.object(self.app, "_read_actual_position_inc", return_value=100), \
             patch.object(self.app, "_disable_drive_worker") as disable, \
             patch.object(self.app, "_command_profile_position") as command:
            self.app._move_absolute_worker(120, self.app._profile_snapshot(), self.app._limits_snapshot())
            disable.assert_called_once()
            command.assert_not_called()

    def test_connected_user_can_start_with_configured_encoder_model(self):
        self.app._post("connected", True)
        self.app._drain_ui_queue()
        self.assertFalse(self.window.advanced.encoder.isEnabled())
        self.assertTrue(self.ui.start.isEnabled())
        self.ui.start.click()
        self.app._safe_start.assert_called_once()

    def test_shift_from_zero_explains_how_to_recapture_the_endpoint(self):
        self._setup_shifted_start(zero=-18000, initial_offset=0)
        labels = [w.text() for w in self.window.confirmation.findChildren(QLabel)]
        self.assertTrue(any("收回端点置零" in label for label in labels))
        self.app._dismiss_start_confirmation()
        self.assertEqual(self.panel.user_zero_um, -18000)
        self.app._command_profile_position.assert_not_called()

    def test_full_stroke_and_zoom_do_not_change_slider_or_limits(self):
        self.panel.show_full_travel()
        self.assertEqual((self.panel.view_min, self.panel.view_max), (-30000, 30000))
        self.panel.target.set("800")
        limits = self.app._limits_snapshot()
        before = self.panel.target_bounds()
        self.panel.span.set("100")
        self.panel.center_view()
        self.assertEqual(self.panel.target_bounds(), before)
        self.assertEqual(self.panel.target.get(), "800")
        self.assertEqual(self.app._limits_snapshot(), limits)
        self.assertEqual((self.panel.view_min, self.panel.view_max), (50, 150))
        self.assertEqual(self.app.travel_range.command_bounds, (-29000, 29000))

    def test_mapping_required_and_cannot_target_reserved_end_zone(self):
        self.panel.target.set("30000")
        self.panel.start()
        self.app._safe_start.assert_not_called()
        self.app.travel_range.invalidate()
        self.panel.target.set("200")
        self.panel.redraw()
        self.assertTrue(self.ui.start.isEnabled())
        self.assertTrue(self.panel.position is not None and self.panel.is_fresh())
        self.assertTrue(self.panel.target.get())

    def test_unmapped_positions_are_visible_and_preview_does_not_move(self):
        self.app.travel_range.invalidate()
        self.feedback(-24975.76)
        self.panel.show_full_travel()
        self.assertTrue(self.panel.position is not None and self.panel.is_fresh())
        self.assertTrue(self.panel.target.get())
        self.assertEqual(self.panel.actual_text.get(), "—")
        self.assertIn("-24975.76 µm", self.panel.encoder_caption.get())
        self.panel.slide_target(-24000)
        self.assertEqual(self.app.target_position_var.get(), "-24000.00")
        self.panel.span.set("1000")
        self.panel.center_view()
        self.assertTrue(self.panel.position is not None and self.panel.is_fresh())
        self.assertTrue(self.panel.target.get())
        self.assertTrue(self.ui.start.isEnabled())
        self.app._safe_start.assert_not_called()

    def test_first_feedback_does_not_fake_physical_midpoint(self):
        self.panel.invalidate()
        self.panel.encoder_view_center = 0.0
        self.feedback(-24975.762)
        self.panel.show_full_travel()
        self.assertEqual(self.panel.encoder_view_center, 0.0)
        self.assertEqual(self.panel.display_bounds(), (-30000, 30000))
        top, bottom = self.ui.plot.y_for(self.panel.view_min), self.ui.plot.y_for(self.panel.view_max)
        expected_y = top + (-24975.762 + 30000) / 60000 * (bottom - top)
        actual = self.ui.plot.y_for(self.panel.position)
        self.assertAlmostEqual(actual, expected_y)
        self.assertLess(actual, (top + bottom) / 2)

    def test_transient_loss_preserves_reading_and_target_with_stale_label(self):
        self.panel.target.set("300")
        self.app._set_status(None)
        self.assertEqual(self.panel.actual_text.get(), "—")
        self.assertIn("上次编码器位置 100.00 µm", self.panel.encoder_caption.get())
        self.assertIn("已过期",self.panel.actual_caption.get())
        self.assertTrue(self.panel.position is not None and not self.panel.is_fresh())
        self.assertEqual(self.panel.target.get(),"300")
        self.feedback(105)
        self.assertTrue(self.panel.position is not None and self.panel.is_fresh())
        self.assertEqual(self.panel.target.get(), "300")

    def test_main_advanced_and_commanded_target_share_coordinates(self):
        self.app.target_position_var.set("250")
        self.assertEqual(self.panel.target.get(),"250")
        self.panel.target.set("350")
        self.assertEqual(self.app.target_position_var.get(),"350")
        self.app._post("target_coordinate", -25001.9674)
        self.app._drain_ui_queue()
        self.assertEqual(self.panel.target.get(),"-25001.97")
        self.assertTrue(self.panel.target.get())
        self.app._safe_start.assert_not_called()

    def test_long_status_keeps_panels_accessible_in_compact_console(self):
        self.window.resize(900,650)
        self.window.show()
        self.app.target_status_var.set("等待控制器状态回复 "*20)
        QApplication.processEvents()
        self.assertTrue(self.window.global_stop.isVisible())
        self.assertTrue(self.window.geometry().size().width()<=900)
        self.assertTrue(self.window.stack.widget(0).widgetResizable())
        self.assertEqual(len(self.ui.presets),7)
        self.assertGreater(self.ui.presets[7].y(),self.ui.presets[1].y())

    def test_motion_setup_uses_current_position_without_switching_pages(self):
        self.window.show_motion_setup()
        self.assertFalse(self.window.setup_dialog.isModal())
        self.panel.use_current_position_as_center()
        self.assertEqual(float(self.panel.center_coordinate.get()),100)
        self.assertTrue(self.panel.center_checked.get())
        next(b for b in self.window.setup_dialog.findChildren(QPushButton) if b.text()=="确认行程映射").click()
        self.assertFalse(self.window.setup_dialog.isVisible())
        self.assertEqual(self.window.stack.currentIndex(),0)
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
        self.assertTrue(not self.ui.start.isEnabled())
        self.assertIn("停止未确认",self.panel.readiness_text.get())
        self.ui.start.click()
        self.app._safe_start.assert_not_called()

    def test_error_notice_does_not_grab_stop_input(self):
        self.window.show_error("测试提示")
        self.assertTrue(self.window.error_dialog.isVisible())
        self.assertIsNone(QApplication.activeModalWidget())
        with patch.object(self.app,"stop_actuator_test") as stop:
            QTest.keyClick(self.window.error_dialog,Qt.Key.Key_Escape)
            stop.assert_called_once()

    def test_notice_centers_on_owner_and_keeps_close_button_on_screen(self):
        self.window.resize(720,560)
        self.window.show()
        QApplication.processEvents()
        self.window.show_error("控制器提示")
        notice=self.window.error_dialog
        area=self.window.screen().availableGeometry()
        self.assertTrue(area.contains(notice.frameGeometry().center()))
        self.window.move(area.right()-80,area.top())
        self.window.show_error("更新后的较长提示。"*15)
        self.assertIs(self.window.error_dialog,notice)
        self.assertLessEqual(notice.frameGeometry().right(),area.right()+2)
        next(b for b in notice.findChildren(QPushButton) if b.text()=="关闭提示").click()
        self.assertFalse(notice.isVisible())

    def test_close_notice_is_reused_and_requires_explicit_power_off_confirmation(self):
        self.app.close_in_progress=True
        with patch.object(self.window,"finish_exit") as exit_app:
            self.app._show_close_notice()
            notice=self.window.close_notice
            buttons={b.text():b for b in notice.findChildren(QPushButton)}
            buttons["隐藏提示"].click()
            self.assertFalse(notice.isVisible())
            self.app._on_close()
            self.assertIs(self.window.close_notice,notice)
            self.assertTrue(notice.isVisible())
            exit_app.assert_not_called()
            buttons["已确认断电并停止，退出软件"].click()
            exit_app.assert_called_once()

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

    def test_disabled_feedback_tracks_manual_adjustment_with_same_zero_and_target(self):
        self.panel.apply_user_zero(8000, time.monotonic())
        self.panel.user_target.set("6000")
        limits = self.app.travel_range.command_bounds
        for raw, shown in ((13000, "5000.00"), (13200, "5200.00")):
            count = self.app._um_to_inc(raw)
            self.feedback(raw, position_inc=count, statusword=0x1250, state="SWITCH ON DISABLED")
            self.assertEqual(self.app.user_position_var.get(), shown)
            self.assertIn(f"{count} 计数", self.panel.encoder_caption.get())
            self.assertEqual(self.panel.user_zero_um, 8000)
            self.assertEqual(float(self.panel.target.get()), 14000)
        self.assertEqual(self.app.travel_range.command_bounds, limits)
        self.app._safe_start.assert_not_called()

    def test_zero_button_rejects_already_enabled_drive(self):
        with patch.object(self.app, "_read_statusword", return_value=0x237), \
             patch.object(self.app, "_write_controlword") as write:
            self.ui.zero_button.click()
            self._finish_submitted_command()
            self.app._show_error.assert_called_once()
            write.assert_not_called()
            self.assertFalse(self.panel.user_zero_set)

    def test_zero_result_applies_only_until_a_stop_request(self):
        payload = {"node": self.node, "axis": "Actuator A", "generation": self.app.cancel_generation,
                   "raw_um": -37000, "sample_time": time.monotonic()}
        self.app._handle_ui_event("user_zero", payload)
        self.assertEqual(self.panel.user_zero_um, -37000)
        self.app.cancel_generation += 1
        self.app._handle_ui_event("user_zero", dict(payload, raw_um=420))
        self.assertEqual(self.panel.user_zero_um, -37000)

    def test_one_zero_click_after_manual_move_then_first_start_uses_refreshed_origin(self):
        app = self.app
        node = SimulatedNode(origin=-27000)
        app.node = app.nodes["Actuator A"] = node
        self.feedback(app._inc_to_um(node.position), statusword=0x40)
        write = node.write
        def refresh_on_enable(index, value):
            write(index, value)
            if index == 0x6040 and value == 0x10F:
                node.position = -35900
        node.write = refresh_on_enable
        self.ui.zero_button.click()
        self._finish_submitted_command()
        self.assertEqual(self.panel.user_zero_um, app._inc_to_um(-35900))
        self.assertEqual(self.panel.actual_text.get(), "0.00")
        self.assertEqual(node.commands, [])
        self.panel.user_target.set("3350")
        with patch.object(app, "_start_motion_trace"):
            self.ui.start.click()
            self._finish_submitted_command()
        app._show_error.assert_not_called()
        self.assertIsNone(app.start_confirmation)
        self.assertEqual([cmd["target_inc"] for cmd in node.commands], [-35900 + app._um_to_inc(3350)])

    def test_endpoint_table_distinguishes_stale_feedback_from_missing_calibration(self):
        self.panel.apply_user_zero(100, time.monotonic())
        self.panel.received_at -= 10
        self.panel.redraw()
        table = self.window.advanced.findChild(TravelReadout)
        self.assertEqual([w.text() for w in table.endpoint_values], ["0.000", "60.000"])
        self.assertEqual([w.text() for w in table.distance_values], ["—", "—"])
        self.assertEqual([w.text() for w in self.ui.summary.distances], ["—", "—"])
        self.app.travel_range.invalidate()
        self.panel.redraw()
        self.assertEqual([w.text() for w in table.endpoint_values], ["—", "—"])
        self.assertEqual(table.coordinate.text(), "待标定")

    def test_endpoint_zero_places_body_at_top_and_derives_range(self):
        self.app.default_limit_var.set(True)
        self.app.travel_range.invalidate()
        self.panel.apply_user_zero(100, time.monotonic())
        self.assertEqual(self.panel.display_bounds(), (100, 15100))
        self.app.default_limit_var.set(False)
        self.panel.show_full_travel()
        self.assertEqual(self.panel.display_bounds(), (100, 60100))
        display = self.window.advanced.findChild(TravelReadout)
        self.assertEqual([w.text() for w in display.endpoint_values], ["0.000", "60.000"])
        self.assertEqual([w.text() for w in display.distance_values], ["0.000", "60.000"])
        before=self.ui.plot.y_for(self.panel.position)
        self.assertEqual(before,self.ui.plot.y_for(self.panel.view_min))
        self.panel.preview_fraction(1.0)
        high_inc = self.app.travel_range.count_bounds(self.app._inc_to_um(1))[1]
        self.assertAlmostEqual(float(self.panel.user_target.get()), self.app._inc_to_um(high_inc) - 100, places=2)
        self.assertEqual(self.ui.plot.y_for(self.panel.position), before)
        self.assertTrue(self.app.travel_range.confirmed)
        self.assertTrue(self.ui.start.isEnabled())
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
        top, bottom = self.ui.plot.y_for(self.panel.view_min), self.ui.plot.y_for(self.panel.view_max)
        self.ui.plot.preview((top+bottom)/2)
        self.assertEqual(float(self.panel.target.get()), 50)
        self.assertEqual(float(self.panel.user_target.get()), -50)
        self.assertFalse(self.app.travel_range.confirmed)
        self.app._safe_start.assert_not_called()
    def test_switching_back_restores_zero_and_review_survives_new_feedback(self):
        self.panel.apply_user_zero(8000, time.monotonic())
        self.app.active_axis_var.set("Actuator B")
        self.assertFalse(self.panel.user_zero_set)
        self.panel.apply_user_zero(3000, time.monotonic())
        self.app.active_axis_var.set("Actuator A")
        self.assertEqual(self.panel.user_zero_um, 8000)
        self.assertEqual(self.panel.center_coordinate.get(), "38000.0000")
        self.feedback(19000)
        self.app._update_workflow_preview()
        self.assertIn("慢速 11000.0 → 13000", self.app.workflow_preview_var.get())
        self.app.reference_review = {"Actuator A": 19000}
        self.app._handle_ui_event("feedback_stale", None)
        self.feedback(19000)
        self.assertEqual(self.panel.user_zero_um, 8000)
        self.assertEqual(self.ui.zero_button.text(), "复核零点")
        with self.assertRaisesRegex(RuntimeError, "待复核"):
            self.app._check_reference_ready()

    def test_axis_switch_preserves_each_axis_mechanical_mapping(self):
        from xeryon_control.domain.travel import TravelRange
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
