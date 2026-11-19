import unittest
from xeryon_control.domain.travel import ROD_LENGTH_UM, STROKE_UM
from xeryon_control.domain.workflow import next_workflow_stage


class WorkflowStageTests(unittest.TestCase):
    def test_boundary_and_linked_axes_choose_conservative_stage(self):
        def plan(positions, retreat=False):
            return next_workflow_stage(positions, 50, 10050, 13000, 5, retreat)
        self.assertEqual(plan([10047, 10052]), (13000, "慢速"))
        self.assertEqual(plan([9990, 10052], True), (50, "慢速"))
        self.assertEqual(plan([11000, 11002], True), (10035, "慢速"))
        self.assertEqual(plan([10035, 10038], True), (50, "快速"))
        self.assertIsNone(plan([12998, 13000]))
        with self.assertRaisesRegex(ValueError, "超出分段范围"):
            plan([49, 13008])

    def test_reverse_direction_uses_signed_distances(self):
        self.assertEqual(next_workflow_stage([2000], 13000, 2950, 50, 5, False), (50, "慢速"))
        self.assertEqual(next_workflow_stage([7000], 13000, 2950, 50, 5, True), (13000, "快速"))


class NominalDimensionsTests(unittest.TestCase):
    def test_rod_length_is_not_stroke(self):
        self.assertEqual(ROD_LENGTH_UM, 95000)
        self.assertEqual(STROKE_UM, 60000)
