import tempfile
import unittest
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from xeryon_control.ui.main_window import MainWindow


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "profile" / "settings.ini"

    def window(self):
        window = MainWindow(settings_file=self.path)
        self.addCleanup(window.finish_exit)
        return window

    def test_real_controls_restore_after_close_without_restoring_session_state(self):
        first = self.window()
        port = first.panel.port.lineEdit()
        port.selectAll()
        QTest.keyClicks(port, "COM41")
        first.panel.speeds[20].click()
        first.panel.dwell.selectAll()
        QTest.keyClicks(first.panel.dwell, "17")
        first.session.invert_b_var.set_from_user(True)
        first.session.position_panel.user_target.set("500")
        first.session.position_panel.apply_user_zero(100, 0)
        first.session.shortcut_selected.append(1)
        first.session.position_panel.speed.set("300")  # shortcut/runtime overwrite
        first.close()
        self.assertTrue(self.path.exists())
        second = self.window()
        self.assertEqual(second.panel.port.currentText(), "COM41")
        self.assertEqual(second.panel.speed.text(), "20")
        self.assertTrue(second.panel.speeds[20].isChecked())
        self.assertEqual(second.panel.dwell.text(), "17")
        self.assertTrue(second.session.invert_b_enabled)
        self.assertFalse(second.session.position_panel.user_zero_set)
        self.assertFalse(second.session.travel_range.confirmed)
        self.assertFalse(second.session.shortcut_selected)
        self.assertIsNone(second.session.network)
        self.assertEqual(float(second.session.target_position_var.get()), 0)

    def test_autosave_and_independent_changes_from_two_windows(self):
        a, b = self.window(), self.window()
        a.session.workflow_end_var.set_from_user("11000")
        b.session.node_id_var.set_from_user("47")
        QTest.qWait(750)
        store = QSettings(str(self.path), QSettings.Format.IniFormat)
        self.assertEqual(store.value("workflow/workflow_end"), "11000")
        self.assertEqual(store.value("connection/node_id"), "47")
        a.session.node_id_var.set("39")  # hardware scan
        b.session.node_id_var.set_from_user("")
        a.finish_exit()
        b.finish_exit()
        self.assertEqual(self.window().session.node_id_var.get(), "47")
