"""Enable initialization fault injection through real application/PDO writers; no CAN."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_hardening import make_app

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from verify_sequence_flow import SimulatedNode


class EnableHoldTests(unittest.TestCase):
    def setup_case(self):
        app = make_app()
        node = SimulatedNode(origin=3685)
        node.rpdo[3]["Target Position"].raw = 0
        node.registers[0x607A] = 0
        app._write_profile = Mock()
        app._disable_drive_worker = Mock()
        app._invalidate_linked_zero = Mock()
        frames = []
        transmit = node.rpdo[3].transmit
        def capture():
            frames.append((node.rpdo[3]["Controlword"].raw,
                           node.rpdo[3]["Target Position"].raw))
            transmit()
        node.rpdo[3].transmit = capture
        return app, node, frames

    def prepare(self, app, node):
        app._prepare_profile_position(node, {"position_window_inc": 5, "position_window_time": 200})

    def test_enable_frame_contains_current_position_not_cached_zero(self):
        app, node, frames = self.setup_case()
        self.prepare(app, node)
        self.assertEqual(frames, [(0, 0), (6, 0), (7, 0), (7, 3685), (15, 3685)])
        self.assertEqual(node.sdo[0x607A].raw, 3685)
        self.assertEqual(node.commands, [])
        app._command_profile_position(node, 3900)
        self.assertEqual([c['target_inc'] for c in node.commands], [3900])

    def test_hold_readback_mismatch_prevents_enable(self):
        app, node, frames = self.setup_case()
        read = node.read
        node.read = lambda index: 0 if index == 0x607A else read(index)
        with self.assertRaisesRegex(RuntimeError, "读回不一致"):
            self.prepare(app, node)
        self.assertFalse(any(word == 15 for word, _ in frames))
        self.assertEqual(node.commands, [])

    def test_read_timeout_prevents_enable(self):
        app, node, frames = self.setup_case()
        read = node.read
        def timeout(index):
            if index == 0x607A:
                raise TimeoutError("hold read timeout")
            return read(index)
        node.read = timeout
        with self.assertRaises(TimeoutError):
            self.prepare(app, node)
        self.assertFalse(any(word == 15 for word, _ in frames))

    def test_cancel_during_readback_prevents_enable(self):
        app, node, frames = self.setup_case()
        read = node.read
        def cancel(index):
            if index == 0x607A:
                app.test_stop_requested.set()
            return read(index)
        node.read = cancel
        with self.assertRaisesRegex(RuntimeError, "运动已取消"):
            self.prepare(app, node)
        self.assertFalse(any(word == 15 for word, _ in frames))

    def test_wrong_state_cannot_overwrite_active_target(self):
        app, node, frames = self.setup_case()
        node.registers[0x6041] = 0x27
        with self.assertRaisesRegex(RuntimeError, "启用前状态不符"):
            app._initialize_enable_hold(node, 3685)
        self.assertEqual(frames, [])

    def test_preexisting_stop_emits_no_frame(self):
        app, node, frames = self.setup_case()
        app.test_stop_requested.set()
        with self.assertRaisesRegex(RuntimeError, "运动已取消"):
            app._initialize_enable_hold(node, 3685)
        self.assertEqual(frames, [])

    def test_pre_enable_jump_invalidates_reference_before_hold_write(self):
        app, node, frames = self.setup_case()
        node.registers[0x6041] = 0x23
        with self.assertRaisesRegex(RuntimeError, "坐标系跳变"):
            app._initialize_enable_hold(node, 3708)
        self.assertEqual(frames, [])
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(app.test_stop_requested.is_set())

    def test_post_enable_jump_still_blocks_motion(self):
        app, node, frames = self.setup_case()
        write = node.write
        def jump(index, value):
            write(index, value)
            if index == 0x6040 and value == 15:
                node.position -= 23
        node.write = jump
        with self.assertRaisesRegex(RuntimeError, "坐标系跳变"):
            self.prepare(app, node)
        self.assertEqual(node.commands, [])
        app._disable_drive_worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
