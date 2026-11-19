import unittest

from xeryon_control.domain.coordinates import AxisOrigins, from_counts, to_counts, to_encoder, to_user


class CoordinateTests(unittest.TestCase):
    def test_negative_encoder_origin_is_shared_by_display_and_target_conversion(self):
        scale = 1000 / 1.0079
        zero = from_counts(-37000, scale)
        target = to_encoder(3350, zero)
        self.assertAlmostEqual(to_user(target, zero), 3350)
        count = to_counts(target, scale)
        self.assertLessEqual(abs(from_counts(count, scale) - target), from_counts(0.5, scale))

    def test_axis_selection_and_disconnect_preserve_zero_semantics(self):
        origins = AxisOrigins()
        origins.capture("A", -37000)
        origins.capture("B", 0)
        origins.select("A")
        self.assertEqual(origins.active_um, -37000)
        origins.forget("A")
        origins.select("B")
        self.assertEqual(origins.active_um, 0)
        origins.clear()
        origins.select("B")
        self.assertIsNone(origins.active_um)
