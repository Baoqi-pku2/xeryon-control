"""Threaded command/STOP integration with a deliberately outstanding SDO upload."""
import threading
import unittest
from unittest.mock import Mock

from tests.test_hardening import make_app
from tools.simulated_node import SimulatedNode


class StopInterruptTests(unittest.TestCase):
    def test_both_axes_receive_disable_before_blocked_command_can_return(self):
        self.exercise_blocked_upload(reply_before_timeout=True)

    def test_unanswered_upload_reports_unconfirmed_stop_and_blocks_restart(self):
        self.exercise_blocked_upload(reply_before_timeout=False)

    def exercise_blocked_upload(self, *, reply_before_timeout):
        app = make_app()
        nodes = [SimulatedNode(origin=100), SimulatedNode(origin=200)]
        nodes[0].id, nodes[1].id = 32, 33
        app.nodes = dict(zip(("Actuator A", "Actuator B"), nodes))
        app.node = nodes[0]
        app.busy_var = Mock()
        app.target_status_var = Mock()
        app._cancel_shortcut_queue = Mock()
        in_upload, release_reply = threading.Event(), threading.Event()
        stop_finished, command_finished = threading.Event(), threading.Event()
        disabled = [threading.Event(), threading.Event()]
        frames = []

        def observe(axis, node):
            pdo = node.rpdo[3]
            transmit = pdo.transmit
            def send():
                frame = (node.id, pdo["Controlword"].raw, pdo["Target Position"].raw)
                frames.append(frame)
                transmit()
                if frame[1] == 0:
                    disabled[axis].set()
            pdo.transmit = send

        for axis, node in enumerate(nodes):
            observe(axis, node)
        read = app.node.read
        def delayed_read(index):
            if index == 0x6064 and not in_upload.is_set():
                in_upload.set()
                if not release_reply.wait(10):
                    raise TimeoutError("test did not release the simulated reply")
            return read(index)
        app.node.read = delayed_read

        def signal_when_done(method, done):
            def run(*args):
                try:
                    return method(*args)
                finally:
                    done.set()
            return run
        app._stop_runner = signal_when_done(app._stop_runner, stop_finished)
        app._command_runner = signal_when_done(app._command_runner, command_finished)

        rejected_writes = []
        def command():
            app._read_actual_position_inc(app.node, fresh=True)
            # Probe both guards even if the first write has already been rejected.
            for write, value in ((app._write_target_position, 8912), (app._write_controlword, 0x0F)):
                try:
                    write(app.node, value)
                except RuntimeError:
                    rejected_writes.append(value)

        app._run_command("模拟等待位置回复", command)
        try:
            self.assertTrue(in_upload.wait(3))
            app.stop_actuator_test()
            for event in disabled:
                self.assertTrue(event.wait(1), "immediate disable waited for the SDO reply")
            self.assertFalse(command_finished.is_set())
            self.assertTrue(app.stop_pending)
            self.assertFalse(app.stop_verified)
            if not reply_before_timeout:
                self.assertTrue(stop_finished.wait(5))
                self.assertTrue(app.stop_unconfirmed)
                self.assertFalse(command_finished.is_set())
        finally:
            release_reply.set()
        self.assertTrue(command_finished.wait(3))
        self.assertTrue(stop_finished.wait(3))
        self.assertEqual(app.stop_verified, reply_before_timeout)
        self.assertEqual(app.stop_unconfirmed, not reply_before_timeout)
        self.assertFalse(app.stop_pending)
        self.assertEqual(app.cancel_generation, 1)
        self.assertEqual(rejected_writes, [8912, 0x0F])
        self.assertTrue(all(word == 0 and target != 8912 for _, word, target in frames))
        verified = [call.kwargs["node_id"] for call in app._record_motion_event.call_args_list
                    if call.args[0] == "stop_verified"]
        self.assertEqual(verified, [32, 33] if reply_before_timeout else [])
        if not reply_before_timeout:
            with self.assertRaisesRegex(RuntimeError, "停止未确认"):
                app._run_command("再次启动", Mock())
