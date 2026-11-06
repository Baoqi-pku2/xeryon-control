"""Progress filtering must reject one-sample spikes without masking a real stall."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from control_gui import XeryonControlGui as Gui
from examples.tests.test_feedback import make_app


class ProgressSpikeTests(unittest.TestCase):
    def test_slow_motion_continues_after_single_forward_spike(self):
        history=[1000,1000]
        best,last=1000,0.0
        old_best,old_last=best,last
        old_stalled=False
        for i in range(101):
            now=i*0.25
            remaining=1000-int(2*now)-(24 if i==40 else 0)
            old_best,old_last,stalled=Gui._update_progress_watchdog(old_best,remaining,old_last,now,1,10)
            old_stalled |= stalled
            best,last,stalled=Gui._update_progress_watchdog(best,Gui._progress_sample(history,remaining),last,now,1,10)
            self.assertFalse(stalled)
        self.assertTrue(old_stalled,'Fixture must reproduce historical-best poisoning')

    def test_fixed_position_still_stops_after_ten_seconds(self):
        history=[800,800];best,last=800,0.0
        for i in range(41):
            now=i*0.25
            best,last,stalled=Gui._update_progress_watchdog(best,Gui._progress_sample(history,800),last,now,1,10)
            self.assertEqual(stalled,now>=10)

    def test_isolated_spikes_cannot_keep_stalled_axis_alive(self):
        history=[800,800];best,last=800,0.0
        for i in range(41):
            remaining=760 if i in (10,20,30,39) else 800
            best,last,stalled=Gui._update_progress_watchdog(best,Gui._progress_sample(history,remaining),last,i*0.25,1,10)
        self.assertTrue(stalled)

    def test_live_recheck_replaces_cached_value_without_double_counting(self):
        history=[100,100]
        Gui._progress_sample(history,20)
        self.assertEqual(Gui._progress_sample(history,99,replace_last=True),100)
        self.assertEqual(history,[100,100,99])

    def test_overshoot_is_not_filtered_or_delayed(self):
        app,node,pdo=make_app(position=120)
        app.active_progress_samples=[100,100]
        app._status_worker()
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(any(c.args[0]=='target' and '越过目标' in str(c.args[1]) for c in app._post.call_args_list))
