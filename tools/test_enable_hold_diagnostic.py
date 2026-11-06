"""Offline checks for a pending diagnostic; no CAN or Tk application is opened."""
import unittest
from types import SimpleNamespace as NS
from diagnose_enable_hold import install_hold_initialization


class EnableHoldDiagnosticTests(unittest.TestCase):
    def setup_case(self, status=0x23, reject=False, cancel=False):
        events=[]; operations=[]
        class Fake:
            def _motion_state(self,node,word,mask,expected):
                operations.append(('state',word))
            def _check_motion_cancelled(self):
                if cancel:
                    raise RuntimeError('cancelled')
            def _read_statusword(self,node,**kwargs):
                return status
            def _read_actual_position_inc(self,node,**kwargs):
                return 3685
            def _write_target_position(self,node,target):
                operations.append(('hold',target))
                if not reject:
                    node.sdo[0x607A].raw=target
        install_hold_initialization(Fake,events)
        return Fake(),NS(id=33,sdo={0x607A:NS(raw=0)}),events,operations

    def test_hold_is_read_back_before_enabling(self):
        app,node,events,operations=self.setup_case()
        app._motion_state(node,0x0F,0x6F,0x27)
        self.assertEqual(operations,[('hold',3685),('state',0x0F)])
        self.assertEqual(events[0]['readback_inc'],3685)

    def test_mismatch_blocks_enable(self):
        app,node,events,operations=self.setup_case(reject=True)
        with self.assertRaisesRegex(RuntimeError,'mismatch'):
            app._motion_state(node,0x0F,0x6F,0x27)
        self.assertNotIn(('state',0x0F),operations)

    def test_enabled_drive_cannot_have_target_overwritten(self):
        app,node,events,operations=self.setup_case(status=0x27)
        with self.assertRaisesRegex(RuntimeError,'disabled'):
            app._motion_state(node,0x0F,0x6F,0x27)
        self.assertEqual(operations,[])

    def test_cancel_does_not_write_target_or_enable(self):
        app,node,events,operations=self.setup_case(cancel=True)
        with self.assertRaisesRegex(RuntimeError,'cancelled'):
            app._motion_state(node,0x0F,0x6F,0x27)
        self.assertEqual(operations,[])

    def test_non_enable_state_transitions_are_unchanged(self):
        app,node,events,operations=self.setup_case()
        app._motion_state(node,0,0x4F,0x40)
        self.assertEqual(operations,[('state',0)])
        self.assertEqual(events,[])


if __name__=='__main__':
    unittest.main(verbosity=2)
