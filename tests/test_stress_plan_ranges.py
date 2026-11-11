"""Manual post-failure batches keep original random cases and failure identity."""
import unittest
from tools.stress_control import generate_plan, select_plan_range


class StressPlanRangeTests(unittest.TestCase):
    def test_selection_preserves_every_original_target_speed_and_dwell(self):
        plan = generate_plan(2026110330, 500)
        selected = select_plan_range(plan, 102, 300)
        original = {s['index']:(s,b['dwell_s']) for b in plan['batches'] for s in b['steps']}
        steps = [s for b in selected['batches'] for s in b['steps']]
        self.assertEqual([s['index'] for s in steps], list(range(1,200)))
        self.assertEqual([s['original_index'] for s in steps], list(range(102,301)))
        for batch in selected['batches']:
            for step in batch['steps']:
                old, dwell = original[step['original_index']]
                self.assertEqual(step['target_user_um'], old['target_user_um'])
                self.assertEqual(step['speed_um_s'], old['speed_um_s'])
                self.assertEqual(batch['dwell_s'], dwell)
        self.assertEqual(plan['count'],500)
        self.assertNotIn('original_index',original[102][0])

    def test_bad_range_fails_and_full_range_stays_identical(self):
        plan=generate_plan(2026110330,500)
        self.assertIs(select_plan_range(plan,1),plan)
        for first,last in ((0,500),(1,501),(200,100)):
            with self.assertRaises(ValueError):select_plan_range(plan,first,last)
