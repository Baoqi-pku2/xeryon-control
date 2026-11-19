"""Event-only persistence and reconnect recovery through real Qt controls; no CAN."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton

from xeryon_control.ui.main_window import MainWindow
from tools.simulated_node import SimulatedNode


class ZeroRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.qt = QApplication.instance() or QApplication([])
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.ini"

    def window(self):
        w = MainWindow(settings_file=self.path)
        for timer in tuple(w._timers):
            w.cancel_timer(timer)
        self.addCleanup(w.deleteLater)
        self.addCleanup(w.finish_exit)
        return w

    def attach(self, w, position=1000, serial=123):
        s = w.session
        s.inc_per_mm = 1000
        node = SimulatedNode(origin=position)
        s.node, s.nodes = node, {"Actuator A": node}
        s.node_identities = {node.id: (1, 2, 3, serial)}
        s._set_connected_state(True)
        self.feedback(s, position)
        return s

    def feedback(self, s, position):
        s._set_status(dict(axis="Actuator A", position_um=position, position_inc=position,
                           sample_time=time.monotonic(), statusword=0x40,
                           state="SWITCH ON DISABLED", mode="PROFILE_POSITION"))

    def drain(self, s):
        while not s.ui_queue.empty():
            s._handle_ui_event(*s.ui_queue.get_nowait())

    def saved_session(self):
        w = self.window()
        s = self.attach(w)
        s.position_panel.apply_user_zero(1000, time.monotonic())
        s.node.position = 4000
        s._disconnect_worker()
        self.drain(s)
        return w

    def accept(self, w):
        next(b for b in w.repeat_dialog.findChildren(QPushButton) if b.text() == "确认").click()

    def test_disconnect_restores_relative_position_with_new_encoder_baseline_without_move(self):
        w = self.saved_session()
        s = self.attach(w, position=-8000)
        self.assertFalse(s.position_panel.user_zero_set)
        s.min_limit_var.set("-9000")
        s.max_limit_var.set("-7000")
        limits = s._limits_snapshot()
        w.refresh()
        self.assertTrue(w.advanced.restore_zero.isEnabled())
        w.advanced.restore_zero.click()
        self.assertFalse(w.repeat_dialog.isModal())
        self.assertFalse(s.position_panel.user_zero_set)
        with patch.object(s, "_safe_start", side_effect=lambda _, work: work()):
            self.accept(w)
        self.drain(s)
        self.assertEqual(s.position_panel.user_zero_um, -11000)
        self.assertEqual(float(s.user_position_var.get()), 3000)
        self.assertEqual(float(s.position_panel.target.get()), -8000)
        self.assertEqual(s._limits_snapshot(), limits)
        self.assertEqual(s.node.commands, [])
        self.assertEqual(s.node.registers[0x6041], 0x40)

    def test_snapshot_uses_final_stopped_position_not_stale_ui(self):
        w = self.saved_session()
        s = self.attach(w)
        record = s.zero_recovery.record()
        self.assertEqual(record["position_inc"], 4000)
        self.assertEqual(s.zero_recovery.coordinate(record), 3000)
        self.assertEqual(record["source"], "停止后读数")
        # Repeated reconnect/disconnect before restoration preserves the record.
        s._disconnect_worker()
        self.drain(s)
        s = self.attach(w)
        self.assertEqual(s.zero_recovery.record(), record)

    def test_feedback_does_not_checkpoint_but_confirmed_stop_does(self):
        w = self.window()
        s = self.attach(w)
        s.position_panel.apply_user_zero(1000, time.monotonic())
        self.drain(s)
        original = s.zero_recovery.serialize()
        with patch.object(w.preferences, "save_zero_recovery", wraps=w.preferences.save_zero_recovery) as save:
            for position in range(2000, 2100):
                self.feedback(s, position)
            self.drain(s)
            save.assert_not_called()
            self.assertEqual(s.zero_recovery.serialize(), original)
            s.node.position = 2099
            s._disable_drive_worker()
            self.drain(s)
            save.assert_called_once()
        restored = self.attach(self.window())
        self.assertEqual(restored.zero_recovery.record()["position_inc"], 2099)
        self.assertFalse(restored.position_panel.user_zero_set)

    def test_identity_and_encoder_scale_isolate_records(self):
        self.saved_session()
        w = self.window()
        s = self.attach(w, serial=456)
        self.assertIsNone(s.zero_recovery.record())
        w.refresh()
        self.assertFalse(w.advanced.restore_zero.isEnabled())
        s.node_identities[s.node.id] = (1, 2, 3, 123)
        s.inc_per_mm = 500
        self.assertIsNone(s.zero_recovery.record())
        s.inc_per_mm = 1000
        self.assertIsNotNone(s.zero_recovery.record())

    def test_stop_or_axis_change_invalidates_confirmation_and_late_result(self):
        self.saved_session()
        for change in ("stop", "axis", "late"):
            with self.subTest(change=change):
                w = self.window()
                s = self.attach(w, position=-8000)
                w.refresh()
                w.advanced.restore_zero.click()
                with patch.object(s, "_safe_start", side_effect=lambda _, work: work()) as start:
                    if change == "stop":
                        s.cancel_generation += 1
                        s.test_stop_requested.set()
                    elif change == "axis":
                        s.active_axis_var.set("Actuator B")
                    self.accept(w)
                    if change == "late":
                        s.cancel_generation += 1
                        s.test_stop_requested.set()
                    else:
                        start.assert_not_called()
                self.drain(s)
                self.assertFalse(s.position_panel.user_zero_set)
                self.assertFalse(s.node.commands)

    def test_corrupt_record_does_not_become_a_zero(self):
        store = QSettings(str(self.path), QSettings.Format.IniFormat)
        store.setValue("recovery/user_zeros", json.dumps({"version": 1, "records": {"bad": {}}}))
        store.sync()
        w = self.window()
        s = self.attach(w)
        self.assertIsNone(s.zero_recovery.record())
        self.assertFalse(s.position_panel.user_zero_set)

    def test_homing_cannot_combine_new_counts_with_old_zero_before_ui_clear(self):
        w = self.window()
        s = self.attach(w)
        s.position_panel.apply_user_zero(1000, time.monotonic())
        old = s.zero_recovery.record()
        s.zero_recovery.suspend_axis("Actuator A")
        s.zero_recovery.capture_stopped({s.node.id: {"position_inc": 5000}})
        self.assertEqual(s.zero_recovery.record(), old)
        s.position_panel.apply_user_zero(5000, time.monotonic())
        self.assertEqual(s.zero_recovery.record()["zero_um"], 5000)

    def test_saved_stop_survives_process_exit_without_close_or_disconnect(self):
        script = r'''
import os, signal, sys, time
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from xeryon_control.ui.main_window import MainWindow
from tools.simulated_node import SimulatedNode
qt = QApplication([])
w = MainWindow(settings_file=sys.argv[1])
s = w.session
s.inc_per_mm = 1000
s.node = SimulatedNode(origin=1000)
s.nodes = {'Actuator A': s.node}
s.node_identities = {s.node.id: (1, 2, 3, 123)}
s.position_panel.apply_user_zero(1000, time.monotonic())
s.node.position = 4000
s._disable_drive_worker()
while not s.ui_queue.empty():
    s._handle_ui_event(*s.ui_queue.get_nowait())
os.kill(os.getpid(), signal.SIGTERM)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertIn(result.returncode, (15, -15), result.stderr.decode(errors="replace"))
        s = self.attach(self.window(), position=-8000)
        self.assertFalse(s.position_panel.user_zero_set)
        self.assertEqual(s.zero_recovery.coordinate(s.zero_recovery.record()), 3000)
