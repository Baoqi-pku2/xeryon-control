"""Qt event routing and visual feedback contracts, using synthetic readings."""

import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from tests.qt_support import create_console
from tools.simulated_node import SimulatedNode


class QtConsoleTests(unittest.TestCase):
    def setUp(self):
        self.window = create_console()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.finish_exit)
        self.s = self.window.session
        self.s.node = SimulatedNode(origin=-3000)
        self.s.nodes = {"Actuator A": self.s.node}

    def feedback(self, position):
        self.s._set_status(
            dict(
                axis="Actuator A",
                sample_time=time.monotonic(),
                position_um=position,
                position_inc=self.s._um_to_inc(position),
                statusword=0x650,
                state="SWITCH ON DISABLED",
                mode="PROFILE_POSITION",
            )
        )

    def test_endpoint_controls_removed_and_user_offset_controls_remain(self):
        texts = [w.text() for w in self.window.findChildren(QLabel)
                 + self.window.findChildren(QPushButton)]
        for caption in ("行程检查", "行程标定", "端点编码器坐标 (µm)",
                        "读取当前位置作为端点", "距收回端", "距伸出端",
                        "确认编码器基准未变，恢复端点"):
            self.assertNotIn(caption, texts)
        for caption in ("当前位置置零", "应用手动偏移", "根据目标位置设置偏移",
                        "写入设备临时限位", "清除设备临时限位"):
            self.assertIn(caption, texts)
        self.assertFalse(hasattr(self.s, "travel_range"))
        self.assertEqual(self.window.panel.summary.stroke_value.text(), "60 mm")

    def test_cia402_state_keeps_device_spelling_on_both_pages(self):
        for state in ("SWITCH ON DISABLED", "READY TO SWITCH ON", "SWITCHED ON", "OPERATION ENABLED"):
            with self.subTest(state=state):
                self.s.state_var.set(state)
                for page in (self.window.panel, self.window.advanced):
                    self.assertIn(state, [label.text() for label in page.findChildren(QLabel)])

    def test_refresh_after_manual_adjustment_updates_readout_without_rezero_or_motion(self):
        s = self.s
        s.network = object()
        panel = s.position_panel
        zero = s._inc_to_um(-3000)
        panel.apply_user_zero(zero, time.monotonic())
        panel.user_target.set("500")
        self.window.refresh()
        write = s.node.write
        def latch(index, value):
            write(index, value)
            if index == 0x6040 and value == 0x10F:
                s.node.position = -2400
        s.node.write = latch
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.window.panel.refresh_button.click()
        while not s.ui_queue.empty():
            s._drain_ui_queue()
        self.assertEqual(panel.position_count, -2400)
        self.assertEqual(panel.user_zero_um, zero)
        self.assertEqual(float(panel.user_target.get()), 500)
        self.assertEqual(self.window.panel.actual.text(), f"+{s._inc_to_um(600):,.2f}")
        self.assertEqual(s.node.registers[0x6041], 0x40)
        self.assertFalse(s.node.commands)

    def test_advanced_refresh_reads_enabled_drive_without_writes(self):
        s = self.s
        s.network = object()
        s.node.registers[0x6041] = 0x27
        s.node.position = -2511
        self.window.refresh()
        with patch.object(s.node, "write", side_effect=AssertionError("unexpected write")), \
             patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.window.advanced.refresh_button.click()
        while not s.ui_queue.empty():
            s._drain_ui_queue()
        self.assertEqual(s.position_panel.position_count, -2511)
        self.assertEqual(s.state_var.get(), "OPERATION ENABLED")

    def test_advanced_negative_user_target_reaches_simulated_drive(self):
        s, panel = self.s, self.s.position_panel
        actual = s._inc_to_um(s.node.position)
        self.feedback(actual)
        limits = s._limits_snapshot()
        limits = s._limits_snapshot()
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.window.panel.zero_button.click()
        s._drain_ui_queue()
        self.assertEqual(panel.user_zero_um, actual)
        self.assertEqual(s._limits_snapshot(), limits)
        self.assertEqual(s._limits_snapshot(), limits)
        self.window.advanced_tab.click()
        entry = next(w for w in self.window.advanced.findChildren(QLineEdit)
                     if w.accessibleName() == "目标位置 (µm)")
        entry.selectAll()
        QTest.keyClicks(entry, "-1000")
        self.assertEqual(entry.text(), "-1000")
        self.assertFalse(self.window.panel.start.isEnabled())
        move = next(b for b in self.window.advanced.findChildren(QPushButton)
                    if b.text() == "移动到目标位置")
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()), \
             patch.object(s, "_start_motion_trace"):
            move.click()
        self.assertEqual([cmd["target_inc"] for cmd in s.node.commands],
                         [s._um_to_inc(actual - 1000)])


    def test_target_sets_current_coordinate_from_settled_sample_without_shifting_limits(self):
        s, panel = self.s, self.s.position_panel
        panel.apply_user_zero(s._inc_to_um(-4000), time.monotonic())
        limits = s._limits_snapshot()
        panel.user_target.set("3000")
        self.window.refresh()
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.window.advanced.offset_buttons[1].click()
        s._drain_ui_queue()
        actual = s._inc_to_um(-3000)
        self.assertAlmostEqual(panel.user_zero_um, actual - 3000)
        self.assertEqual(float(panel.user_target.get()), 3000)
        self.assertEqual(float(panel.target.get()), actual)
        self.assertEqual(float(s.user_position_var.get()), 3000)
        self.assertEqual(s._limits_snapshot(), limits)
        self.assertFalse(s.node.commands)


    def test_manual_negative_coordinate_survives_axis_switch(self):
        s, panel = self.s, self.s.position_panel
        self.feedback(s._inc_to_um(-3000))
        panel.offset_coordinate.set("-125")
        self.window.refresh()
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.window.advanced.offset_buttons[0].click()
        s._drain_ui_queue()
        self.assertEqual(float(s.user_position_var.get()), -125)
        self.assertFalse(s.node.commands)
        s.active_axis_var.set("Actuator B")
        self.assertFalse(panel.user_zero_set)
        s.active_axis_var.set("Actuator A")
        self.assertAlmostEqual(panel.user_zero_um, s._inc_to_um(-3000) + 125)

    def test_user_edit_survives_feedback_and_page_switch(self):
        self.feedback(-3000)
        self.s.position_panel.apply_user_zero(-3000, time.monotonic())
        entry = self.window.panel.entry
        entry.selectAll()
        QTest.keyClicks(entry, "235.5")
        self.feedback(-2900)
        self.window.advanced_tab.click()
        self.assertEqual(self.window.stack.count(), 3)
        self.window.console_tab.click()
        self.assertEqual(entry.text(), "235.5")
        self.assertEqual(float(self.s.target_position_var.get()), -2764.5)
        self.assertEqual(self.window.panel.actual.text(), "+100.00")
        self.assertEqual(self.s.node.commands, [])
        self.assertEqual(
            [self.window.advanced.tabText(i) for i in range(6)],
            [
                "高级控制",
                "设备连接",
                "运动参数",
                "重复定位 / 测试",
                "零点 / 限位",
                "运行日志",
            ],
        )

    def test_escape_with_focus_on_confirmation_button_requests_stop(self):
        s = self.s
        with patch.object(s, "_run_stop_command") as stop:
            self.window.show_confirmation(
                "起点位置已变化", "编码器绝对位置", "", object()
            )
            QTest.keyClick(self.window.continue_button, Qt.Key.Key_Escape)
            stop.assert_called_once_with("STOP")
            self.assertTrue(s.test_stop_requested.is_set())

    def test_repeatability_confirmation_cannot_resume_after_stop(self):
        self.s.start_repeatability()
        self.assertFalse(self.window.repeat_dialog.isModal())
        with (
            patch.object(self.s, "_stop_runner") as stop,
            patch.object(self.s, "_safe_start") as start,
        ):
            QTest.keyClick(self.window.repeat_dialog, Qt.Key.Key_Escape)
            self.assertEqual(self.s.cancel_generation, 1)
            self.assertTrue(self.s.stop_pending)
            next(
                b
                for b in self.window.repeat_dialog.findChildren(QPushButton)
                if b.text() == "确认"
            ).click()
            start.assert_not_called()
            self.assertIsNone(QApplication.activeModalWidget())
