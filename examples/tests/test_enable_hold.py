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
        samples = [c.args[1] for c in app._post.call_args_list if c.args[0] == "log" and
                   c.args[1].startswith("启动采样")]
        stages = [("失能已确认", 0x40), ("准备就绪", 0x21), ("Switched On 已确认", 0x23),
                  ("使能前：写入保持目标前", 0x23), ("使能刚确认", 0x27), ("位置模式反馈已确认", 0x27)]
        self.assertEqual(len(stages), len(samples))
        for sample, (stage, status) in zip(samples, stages):
            self.assertIn(f"节点 {node.id} · {stage} · 状态字 0x{status:04X}", sample)
            self.assertIn("0x6064 = 3685 计数", sample)
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

    def test_pre_enable_displacement_stops_before_hold_write(self):
        app, node, frames = self.setup_case()
        node.registers[0x6041] = 0x23
        with self.assertRaisesRegex(RuntimeError, "意外位移"):
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
        with self.assertRaisesRegex(RuntimeError, "意外位移"):
            self.prepare(app, node)
        self.assertEqual(node.commands, [])
        app._disable_drive_worker.assert_called_once()
        self.assertTrue(any("使能刚确认" in str(c) and "0x6064 = 3662" in str(c)
                            and "-23 计数" in str(c) for c in app._post.call_args_list))

    def zero_case(self):
        app, node, frames = self.setup_case()
        app.node = node
        app.nodes = {"Actuator A": node}
        del app._disable_drive_worker  # Exercise the real disable and settle verification.
        return app, node, frames

    def test_zero_uses_enabled_position_then_verified_stop_without_a_setpoint(self):
        app, node, frames = self.zero_case()
        write = node.write
        def refresh(index, value):
            write(index, value)
            if index == 0x6040 and value == 0x10F:
                node.position -= 8900
        node.write = refresh
        app._capture_user_zero_worker(node, "Actuator A")
        self.assertEqual([word for word, _ in frames], [0, 6, 7, 7, 0x10F, 0])
        self.assertEqual(frames[-2][1], 3685)
        self.assertFalse(node.commands)
        self.assertEqual(node.registers[0x6041], 0x40)
        self.assertEqual(app._post.call_args.args[0], "user_zero")
        self.assertEqual(app._post.call_args.args[1]["raw_um"], -5215)

    def test_zero_aborts_on_drift_read_failure_stop_or_failed_disable(self):
        for fault in ("drift", "read", "stop", "disable", "stop_drift", "halt", "mode"):
            with self.subTest(fault=fault):
                app, node, _ = self.zero_case()
                read = node.read
                def inject(index):
                    if node.registers[0x6041] & 0x6F == 0x27:
                        if fault == "halt" and index == 0x6040:
                            return 0x0F
                        if fault == "mode" and index == 0x6061:
                            return 6
                    if node.registers[0x6041] & 0x6F == 0x27 and index == 0x6064:
                        if fault == "read":
                            raise TimeoutError("SDO read failed")
                        if fault == "stop":
                            app.test_stop_requested.set()
                        if fault == "drift":
                            node.position += 3
                        if fault == "stop_drift":
                            return node.position + 3
                    return read(index)
                node.read = inject
                if fault == "disable":
                    app._disable_nodes = Mock(side_effect=RuntimeError("停止未确认"))
                with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                    app._capture_user_zero_worker(node, "Actuator A")
                if fault == "disable":
                    self.assertTrue(app.stop_unconfirmed)
                else:
                    self.assertEqual(node.registers[0x6041], 0x40)
                self.assertFalse(node.commands)
                self.assertFalse(any(call.args[0] == "user_zero" for call in app._post.call_args_list))

    def test_enable_compares_against_actual_hold_even_if_it_returns_to_old_reading(self):
        app, node, _ = self.setup_case()
        write = node.write
        def shift(index, value):
            write(index, value)
            if index == 0x6040 and value == 7:
                node.position = 3703
            if index == 0x6040 and value == 15:
                node.position = 3675
        node.write = shift
        with self.assertRaisesRegex(RuntimeError, "意外位移"):
            self.prepare(app, node)
        self.assertFalse(node.commands)
        app._disable_drive_worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
