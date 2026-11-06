"""Finite ramp programming without altering acceleration, target or limits."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_gui import XeryonControlGui as Gui


class ProfileJerkTests(unittest.TestCase):
    def make(self):
        app = Gui.__new__(Gui)
        app.inc_per_mm = 1000/1.0079
        app._post = Mock()
        names = ('Profile velocity','Profile acceleration','Max profile velocity','Max acceleration',
                 'Quick stop deceleration','Following error window','Following error time out')
        sdo = {name: NS(raw=0) for name in names}
        sdo[0x60A4] = {1: NS(raw=992161920)}
        profile = dict(velocity=300, acceleration=50, velocity_inc=298, acceleration_inc=50,
                       max_velocity_inc=298, max_acceleration_inc=496, quick_stop_decel_inc=496,
                       following_error_window_inc=20, following_error_timeout=1000)
        return app, NS(sdo=sdo), profile

    def test_resolved_ramp_matches_successful_real_device_parameters(self):
        app, node, profile = self.make()
        app._write_profile(node, profile)
        self.assertEqual(node.sdo[0x60A4][1].raw, 496)
        self.assertEqual(node.sdo['Profile acceleration'].raw, 50)
        self.assertEqual(node.sdo['Profile velocity'].raw, 298)
        self.assertEqual(profile['jerk'], 500)
        self.assertEqual(profile['acceleration'], 50)

    def test_jerk_recomputed_after_profile_acceleration_override(self):
        app, node, profile = self.make()
        profile.update(acceleration=100, acceleration_inc=99, jerk=500, jerk_inc=496)
        app._write_profile(node, profile)
        self.assertEqual(node.sdo[0x60A4][1].raw, 992)
        self.assertEqual(profile['jerk'], 1000)
        self.assertEqual(node.sdo['Profile acceleration'].raw, 99)

    def test_readback_mismatch_fails_before_other_profile_writes(self):
        app, node, profile = self.make()
        class RejectedWrite:
            @property
            def raw(self): return 992161920
            @raw.setter
            def raw(self, value): pass
        node.sdo[0x60A4][1] = RejectedWrite()
        with self.assertRaisesRegex(RuntimeError, '读回不一致'):
            app._write_profile(node, profile)
        self.assertEqual(node.sdo['Profile acceleration'].raw, 0)

    def test_unsupported_jerk_does_not_silently_use_old_parameters(self):
        app, node, profile = self.make()
        del node.sdo[0x60A4]
        with self.assertRaises(KeyError):
            app._write_profile(node, profile)
        self.assertEqual(node.sdo['Profile velocity'].raw, 0)


if __name__ == '__main__':
    unittest.main()
