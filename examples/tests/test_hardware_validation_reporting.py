"""Preflight failure reports must not label the last UI position as physical truth."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from run_hardware_validation import apply_verified_shutdown


class HardwareValidationReportingTests(unittest.TestCase):
    def test_preflight_failure_uses_confirmed_stop_position(self):
        report={'result':'FAILED','final_position_inc':-96}
        apply_verified_shutdown(report,True,{33:{'position_inc':-139,'statusword':1616}})
        self.assertEqual(report['final_position_inc'],-139)
        self.assertEqual(report['last_ui_position_inc'],-96)
        self.assertEqual(report['result'],'FAILED')

    def test_missing_stop_evidence_does_not_claim_ui_position(self):
        report={'result':'FAILED','final_position_inc':-96}
        apply_verified_shutdown(report,True,{})
        self.assertIsNone(report['final_position_inc'])
        self.assertEqual(report['final_position_source'],'unverified')

    def test_unconfirmed_shutdown_cannot_pass_or_claim_stop_position(self):
        report={'result':'PASSED','final_position_inc':-96}
        apply_verified_shutdown(report,False,{33:{'position_inc':-139,'statusword':1616}})
        self.assertEqual(report['result'],'SHUTDOWN_UNCONFIRMED')
        self.assertIsNone(report['final_position_inc'])

    def test_pass_requires_verified_stop_evidence(self):
        report={'result':'PASSED','final_position_inc':100}
        apply_verified_shutdown(report,True,{})
        self.assertEqual(report['result'],'SHUTDOWN_UNCONFIRMED')
