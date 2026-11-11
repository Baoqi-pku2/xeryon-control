import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_range import TravelRange, ROD_LENGTH_UM, STROKE_UM, next_workflow_stage


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


class TravelRangeTests(unittest.TestCase):
    def test_rod_length_is_not_stroke(self):
        self.assertEqual(ROD_LENGTH_UM, 95000)
        self.assertEqual(STROKE_UM, 60000)

    def test_unknown_reference_blocks_motion(self):
        with self.assertRaises(ValueError):
            TravelRange().validate(0, 0, 1.0079, "Actuator A")

    def test_offset_origin_and_reserved_ends(self):
        travel = TravelRange()
        travel.confirm(1200, 1000, "Actuator A")
        self.assertEqual(travel.mechanical_bounds, (-28800, 31200))
        self.assertEqual(travel.command_bounds, (-27800, 30200))
        travel.validate(1500, round(1500/1.0079), 1.0079, "Actuator A")
        for target in (-27801, 30201):
            with self.assertRaises(ValueError):
                travel.validate(target, round(target/1.0079), 1.0079, "Actuator A")

    def test_count_rounding_cannot_cross_limit(self):
        travel = TravelRange()
        travel.confirm(0.1, 1000, "A")
        low, high = travel.count_bounds(1.0079)
        for count in (low, high):
            travel.validate(count*1.0079, count, 1.0079, "A")
        for count in (low-1, high+1):
            with self.assertRaises(ValueError):
                travel.validate(count*1.0079, count, 1.0079, "A")

    def test_requested_fifty_um_margin_is_supported(self):
        travel = TravelRange()
        travel.confirm(30100, 50, "Actuator A")
        self.assertEqual(travel.command_bounds, (150, 60050))
        travel.validate(150, round(150/1.0079), 1.0079, "Actuator A")

    def test_range_error_reports_actual_target_count_and_axis(self):
        travel = TravelRange()
        travel.confirm(29858.89, 50, "Actuator A")
        with self.assertRaisesRegex(ValueError, "实际目标 60000.00.*60000 计数.*Actuator A"):
            travel.validate(60000, 60000, 1, "Actuator A")

    def test_wrong_axis_and_invalid_geometry_rejected(self):
        travel = TravelRange()
        travel.confirm(0, 1000, "A")
        with self.assertRaises(ValueError):
            travel.validate(0, 0, 1.0079, "B")
        for center, margin in ((float('nan'),1000),(0,0),(0,30000),(0,float('inf'))):
            with self.assertRaises(ValueError):
                travel.confirm(center, margin, "A")
