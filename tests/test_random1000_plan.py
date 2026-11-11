"""Acceptance plan preserves scope and cannot disguise failed or partial runs."""
import unittest
from tools.plan_random1000 import generate


class Random1000PlanTests(unittest.TestCase):
    def test_scope_and_exactly_one_thousand_numbered_targets(self):
        plan=generate()
        steps=[s for b in plan['batches'] for s in b['steps']]
        self.assertEqual([s['index'] for s in steps],list(range(1,1001)))
        self.assertTrue(all(100<=s['target_user_um']<=12000 and 2<=s['speed_um_s']<=500 for s in steps))
        self.assertEqual(set(b['kind'] for b in plan['batches']),{'single','sequence'})
        self.assertTrue(all(0<=b['dwell_s']<=30 and 1<=len(b['steps'])<=7 for b in plan['batches']))
        self.assertTrue(any(b['dwell_s']==30 for b in plan['batches']))
        self.assertTrue(any(s['speed_um_s']==2 for s in steps))
        self.assertTrue(any(s['speed_um_s']==500 for s in steps))
        current=100
        for step in steps:
            self.assertEqual(step['planned_distance_um'],abs(step['target_user_um']-current))
            current=step['target_user_um']

    def test_reproducible_and_zero_failure_acceptance(self):
        self.assertEqual(generate(),generate())
        acceptance=generate()['acceptance']
        self.assertEqual(acceptance['completed_count'],1000)
        self.assertEqual(acceptance['allowed_failures'],0)
        self.assertFalse(acceptance['retry_replaces_failure'])
        self.assertFalse(acceptance['resume_after_failure_can_pass'])
