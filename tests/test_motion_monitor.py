import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.qt_support import create_console
from tools.simulated_node import SimulatedNode
from xeryon_control.application.monitoring import MonitorSegment, MotionCapture
from xeryon_control.domain.kinematics import EncoderSmoother


class EncoderMonitorTests(unittest.TestCase):
    def test_rts_recovers_irregularly_sampled_quadratic_and_flushes_tail(self):
        estimator = EncoderSmoother(1)
        output = []
        for n in range(60):
            t = n / 10 + (n % 3) / 100
            output.extend(estimator.add(t, round(200 - 20*t + 25*t*t)))
        self.assertLessEqual(output[-1][0], t-estimator.lag)
        output.extend(estimator.finish())
        self.assertEqual(len(output), 60)
        for t, p, v, a in output[10:-10]:
            self.assertEqual(p, round(200 - 20*t + 25*t*t))
            self.assertAlmostEqual(v, -20 + 50*t, delta=2)
            self.assertAlmostEqual(a, 50, delta=6)

    def test_new_frames_with_equal_counts_and_sdo_fallback(self):
        events = []
        capture = MotionCapture(lambda *event: events.append(event))
        node = object()
        started = time.monotonic()
        seg = MonitorSegment(started, "Actuator A", "开始运动", 2., 10., True)
        capture.begin(node, seg, 8, started)
        class Frame(dict):
            pass
        frame = Frame({"Position Actual Value": SimpleNamespace(raw=8)})
        with patch("xeryon_control.application.monitoring.monotonic", return_value=started+1):
            frame.timestamp = capture.wall_offset + started + 0.1
            capture.on_pdo(node, frame)
            capture.on_pdo(node, frame)
            capture.on_sdo(node, 8, started+0.15)
            frame.timestamp += 0.1
            capture.on_pdo(node, frame)
            capture.on_sdo(node, 7, started+0.6)
            capture.finish()
            capture.on_pdo(node, frame)
        self.assertEqual([payload[2] for kind, payload in events if kind == "monitor_sample"], [6, 6, 6, 4])
        self.assertEqual(events[-1], ("monitor_end", seg))


class MonitorWidgetTests(unittest.TestCase):
    def setUp(self):
        self.window = create_console()
        self.s = self.window.session
        self.page = self.window.monitor

    def tearDown(self):
        self.window.finish_exit()
        self.window.deleteLater()

    def samples(self, click, name, offset=0):
        seg = MonitorSegment(click, "Actuator A", name, 1., 0., True)
        for i in range(30):
            self.page.receive("monitor_sample", (seg, click+offset+i/10, (i-15)**2))
        self.page.receive("monitor_end", seg)
        return seg

    def test_segments_shared_origin_color_axes_retention_and_clear(self):
        self.window.resize(900, 640)
        self.window.show()
        self.window.monitor_tab.click()
        now = time.monotonic()
        a = self.samples(now-20, "快捷 1")
        b = self.samples(now-20, "快捷 2", 4)
        self.assertEqual(self.page.series[b].curves[0].bounds[0], 4.)
        self.assertNotEqual(self.page.series[a].color, self.page.series[b].color)
        self.page.axis.setCurrentIndex(1)
        self.assertEqual([c.by_position for c in self.page.charts], [False, True, True])
        for chart in self.page.charts:
            self.assertFalse(chart.grab().isNull())
        self.page.count.setValue(1)
        self.assertEqual(len(self.page.series), 2)
        c = self.samples(now-3, "开始运动")
        self.assertEqual(list(self.page.series), [c])
        self.page.clear_button.click()
        self.page.receive("monitor_sample", (c, now, 1))
        self.assertFalse(self.page.series)
        self.assertTrue(self.window.global_stop.isVisible())

    def test_start_feedback_and_escape_through_console_and_monitor(self):
        s = self.s
        node = SimulatedNode(origin=-2000)
        s.node = node
        s.nodes = {"Actuator A": node}
        s.position_panel.apply_user_zero(s._inc_to_um(node.position), time.monotonic())
        s._set_status(dict(axis="Actuator A", position_um=s._inc_to_um(node.position),
                           position_inc=node.position, sample_time=time.monotonic(),
                           state="SWITCH ON DISABLED", statusword=0x40, mode="PROFILE_POSITION"))
        self.window.show()
        s.position_panel.user_target.set("3000")
        s.position_panel.speed.set("2")
        with patch.object(s, "_start_motion_trace"), patch.object(s, "_show_error") as error:
            self.window.panel.start.click()
            self.wait_until(lambda: bool(node.commands) and not s.command_busy)
            error.assert_not_called()
            self.window.monitor_tab.click()
            for _ in range(4):
                s._status_worker()
                s._drain_ui_queue()
                QTest.qWait(15)
            self.assertEqual(len(self.page.series), 1)
            QTest.keyClick(self.page.axis, Qt.Key.Key_Escape)
            self.wait_until(lambda: s.stop_verified and not s.stop_pending)
            curves = next(iter(self.page.series.values())).curves
            self.assertAlmostEqual(curves[0].bounds[2], 0., places=6)
            self.assertTrue(all(curve.bounds is not None for curve in curves))
            self.assertIsNone(s.motion_capture.node)
            self.assertEqual(len(node.commands), 1)

    def wait_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            while not self.s.ui_queue.empty():
                self.s._handle_ui_event(*self.s.ui_queue.get_nowait())
            QApplication.processEvents()
            time.sleep(0.01)
        while not self.s.ui_queue.empty():
            self.s._handle_ui_event(*self.s.ui_queue.get_nowait())
        self.assertTrue(condition())
