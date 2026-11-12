"""Qt event routing and visual feedback contracts, using synthetic readings."""

import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

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

    def test_user_edit_survives_feedback_and_page_switch(self):
        self.feedback(-3000)
        self.s.position_panel.apply_user_zero(-3000, time.monotonic())
        entry = self.window.panel.entry
        entry.selectAll()
        QTest.keyClicks(entry, "235.5")
        self.feedback(-2900)
        self.window.advanced_tab.click()
        self.assertEqual(self.window.stack.count(), 2)
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
                "行程 / 零点",
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
