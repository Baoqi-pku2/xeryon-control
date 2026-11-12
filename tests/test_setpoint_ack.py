"""Setpoint acceptance, stale acknowledgements and STOP while waiting; no CAN."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from xeryon_control.application.session import ControllerSession as Gui


class SetpointAcknowledgementTests(unittest.TestCase):
    def app(self):
        app = Gui.__new__(Gui)
        app.test_stop_requested = threading.Event()
        app.cancel_generation = 0
        app.stop_unconfirmed = False
        app._write_controlword = Mock()
        app._write_target_position = Mock()
        return app

    def test_single_setpoint_waits_for_live_acceptance(self):
        app = self.app()
        node = NS(id=33)
        app._read_statusword = Mock(side_effect=[0x27, 0x27, 0x1027])
        app._command_profile_position(node, 123)
        self.assertEqual(app._read_statusword.call_count, 3)
        self.assertTrue(all(c.kwargs == {'fresh': True} for c in app._read_statusword.call_args_list))
        self.assertEqual([c.args[1] for c in app._write_controlword.call_args_list], [0x0F, 0x1F, 0x0F])
        app._write_target_position.assert_called_once_with(node, 123)

    def test_idle_latched_ack_is_supported_as_on_real_controller(self):
        app = self.app()
        app._read_statusword = Mock(return_value=0x1027)
        app._command_profile_position(NS(id=33), 123)
        self.assertEqual(app._write_controlword.call_count, 3)

    def test_missing_acceptance_does_not_report_success(self):
        app = self.app()
        app._read_statusword = Mock(return_value=0x27)
        with patch('xeryon_control.application.session.SETPOINT_ACK_TIMEOUT_SECONDS', 0), self.assertRaisesRegex(RuntimeError, '握手超时'):
            app._command_profile_position(NS(id=33), 123)

    def test_fault_or_disabled_drive_rejected(self):
        for status in (0x08, 0x2027, 0x40):
            with self.subTest(status=status):
                app = self.app()
                app._read_statusword = Mock(return_value=status)
                with self.assertRaisesRegex(RuntimeError, '故障或未开启'):
                    app._command_profile_position(NS(id=33), 123)
                self.assertEqual(app._write_controlword.call_count, 2)

    def test_stop_during_sdo_read_prevents_followup_writes(self):
        app = self.app()
        def read(*args, **kwargs):
            app.test_stop_requested.set()
            return 0x27
        app._read_statusword = read
        with self.assertRaisesRegex(RuntimeError, '取消'):
            app._command_profile_position(NS(id=33), 123)
        self.assertEqual(app._write_controlword.call_count, 2)

    def test_sdo_error_propagates_for_outer_stop_cleanup(self):
        app = self.app()
        app._read_statusword = Mock(side_effect=OSError('CAN disconnected'))
        with self.assertRaises(OSError):
            app._command_profile_position(NS(id=33), 123)
        self.assertEqual(app._write_controlword.call_count, 2)

    def test_both_axes_must_acknowledge(self):
        app = self.app()
        first, second = NS(id=32), NS(id=33)
        values = {32: iter([0x1027]), 33: iter([0x27, 0x1027])}
        app._read_statusword = Mock(side_effect=lambda node, **kw: next(values[node.id]))
        app._wait_setpoint_ack([first, second], True)
        self.assertEqual([c.args[0].id for c in app._read_statusword.call_args_list], [32, 33, 33])


if __name__ == '__main__':
    unittest.main()
