"""Exercise STOP with lost replies, busy I/O and late motion writes; no hardware."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xeryon_control.ui.main_window import XeryonControlGui as Gui
from xeryon_control.hardware.stop import verify_disabled


class Sdo(dict):
    RESPONSE_TIMEOUT = 1.0
    MAX_RETRIES = 3


def node(status=0x40, position=100):
    return SimpleNamespace(id=32, sdo=Sdo({
        0x6040: SimpleNamespace(raw=0x0F),
        0x6041: SimpleNamespace(raw=status),
        0x6064: SimpleNamespace(raw=position),
    }))


def app():
    a = Gui.__new__(Gui)
    a.io_lock = threading.RLock()
    a.controlword_lock = threading.Lock()
    a.test_stop_requested = threading.Event()
    a._init_stop_control()
    a.cancel_generation = 0
    a.nodes = {"A": node()}
    a.node = a.nodes["A"]
    a.active_target_inc = 1000
    a.active_start_inc = 0
    a._post = Mock()
    a._record_motion_event = Mock()
    a._log = Mock()
    a.target_status_var = Mock()
    a.after = Mock()
    a._show_error = Mock()
    return a


class StopTests(unittest.TestCase):
    def test_live_disabled_and_stable_required(self):
        n = node()
        result = verify_disabled(n, settle=0.05)
        self.assertEqual(result, {"statusword": 0x40, "position_inc": 100})
        self.assertEqual(n.sdo[0x6040].raw, 0)
        self.assertEqual((n.sdo.RESPONSE_TIMEOUT,n.sdo.MAX_RETRIES),(1.0,3))

    def test_enabled_and_quick_stop_states_are_not_disabled(self):
        for status in (0x27, 0x07, 0x0F, 0x00):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                verify_disabled(node(status=status), timeout=0.06, settle=0.01)

    def test_moving_encoder_is_not_stopped(self):
        class Moving:
            @property
            def raw(self):
                self.count = getattr(self,"count",0) + 2
                return self.count
        n = node()
        n.sdo[0x6064] = Moving()
        with self.assertRaises(RuntimeError):
            verify_disabled(n, timeout=0.1, settle=0.01)

    def test_missing_reply_is_failure_and_restores_timeouts(self):
        n = node()
        del n.sdo[0x6041]
        with self.assertRaises(KeyError):
            verify_disabled(n)
        self.assertEqual((n.sdo.RESPONSE_TIMEOUT,n.sdo.MAX_RETRIES),(1.0,3))

    def test_disable_never_sends_enable_and_verifies_every_axis(self):
        a = app()
        a._write_controlword = Mock()
        other = node()
        other.id = 33
        with patch('xeryon_control.application.stop.verify_disabled', return_value={}) as verify:
            a._disable_nodes([a.node, other])
        self.assertEqual([c.args[1] for c in a._write_controlword.call_args_list],[0,0])
        self.assertEqual(verify.call_count,2)

    def test_failed_disable_never_reports_success(self):
        a = app()
        a._disable_nodes = Mock(side_effect=RuntimeError('no reply'))
        with self.assertRaises(RuntimeError):
            a._disable_drive_worker()
        self.assertTrue(a.stop_unconfirmed)
        self.assertFalse(any('已停止' in str(c) or 'Disabled' in str(c) for c in a._post.call_args_list))

    def test_stop_frame_precedes_wait_for_busy_io(self):
        a = app()
        sent = threading.Event()
        a._write_controlword = Mock(side_effect=lambda *args: sent.set())
        a._stop_and_disable_worker = Mock()
        with a.io_lock:
            worker = threading.Thread(target=a._stop_runner,args=('STOP',))
            worker.start()
            self.assertTrue(sent.wait(0.5), 'STOP waited behind I/O lock')
            a._stop_and_disable_worker.assert_not_called()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        a._stop_and_disable_worker.assert_called_once()

    def test_stop_interrupts_mode_wait_and_confirms_disable(self):
        a = app()
        a._initialize_enable_hold = Mock()
        a.inc_per_mm = 1000
        a.node.sdo.update({
            "Mode of operation": SimpleNamespace(raw=0),
            0x6061: SimpleNamespace(raw=0),
            "Position window": SimpleNamespace(raw=0),
            "Position window time": SimpleNamespace(raw=0),
        })
        a._write_profile = Mock()
        a._write_controlword = Mock()
        a._stop_and_disable_worker = Mock()
        a._read_actual_position_inc = Mock(return_value=100)
        worker = None
        def request_stop(node, controlword, mask, expected):
            nonlocal worker
            if controlword == 0x0F:
                a.stop_pending = True
                a.cancel_generation += 1
                a.test_stop_requested.set()
                worker = threading.Thread(target=a._stop_runner, args=("STOP",))
                worker.start()
            return expected
        a._motion_state = Mock(side_effect=request_stop)
        with a.io_lock:
            with self.assertRaisesRegex(RuntimeError, "运动已取消"):
                a._prepare_profile_position(
                    a.node, {"position_window_inc": 5, "position_window_time": 200})
        worker.join(1)
        self.assertFalse(worker.is_alive())
        a._stop_and_disable_worker.assert_called_once()
        self.assertFalse(a.stop_unconfirmed)

    def test_dual_axis_stop_owns_real_delayed_cleanup(self):
        from tests.test_hardening import linked_app
        class Cell:
            def __init__(self, value, delay=0):
                self.value = value
                self.delay = delay
            @property
            def raw(self):
                time.sleep(self.delay)
                return self.value
            @raw.setter
            def raw(self, value):
                self.value = value
        def delayed_node(node_id):
            return SimpleNamespace(id=node_id, sdo=Sdo({
                0x6040: Cell(0x0F), 0x6041: Cell(0x27, 0.26), 0x6064: Cell(100, 0.26),
            }))
        a = linked_app()
        nodes = {"Actuator A": delayed_node(32), "Actuator B": delayed_node(33)}
        a.nodes = nodes
        a.node = nodes["Actuator A"]
        del a._disable_nodes
        a._read_actual_position_inc = Mock(return_value=100)
        worker = None
        def fail_second_axis(node, profile, reference):
            nonlocal worker
            if node.id == 33:
                a.stop_pending = True
                a.cancel_generation += 1
                a.test_stop_requested.set()
                worker = threading.Thread(target=a._stop_runner, args=("STOP",))
                worker.start()
                raise RuntimeError("模式等待取消")
        a._prepare_profile_position = Mock(side_effect=fail_second_axis)
        link = {"enabled": True, "invert_b": False,
                "offsets": a.linked_zero_offsets_inc, "tolerance_um": 20}
        def transport(node, value):
            node.sdo[0x6040].raw = value
            node.sdo[0x6041].value = 0x40 if value == 0 else 0x27
        with patch("xeryon_control.application.commands.set_controlword", side_effect=transport), \
             patch("xeryon_control.application.stop.verify_disabled", wraps=verify_disabled) as verified, \
             patch.object(a, "_stop_and_disable_worker", wraps=a._stop_and_disable_worker) as stopped:
            a._command_runner("linked STOP", lambda: a._run_linked_move(
                {"Actuator A": 1000, "Actuator B": 1000}, {"position_window_inc": 50}, 1, link,
                starts_inc={"Actuator A": 100, "Actuator B": 100}), 0)
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(stopped.call_count, 1, a._post.call_args_list)
        self.assertEqual(verified.call_count, 2)
        a._command_profile_positions.assert_not_called()
        self.assertFalse(a.stop_unconfirmed)

    def test_dual_axis_feedback_stop_skips_final_reads_and_completes_cleanup(self):
        from tests.test_hardening import linked_app
        class Cell:
            def __init__(self, value, delay=0):
                self.value = value
                self.delay = delay
            @property
            def raw(self):
                time.sleep(self.delay)
                return self.value
            @raw.setter
            def raw(self, value):
                self.value = value
        def delayed_node(node_id):
            return SimpleNamespace(id=node_id, sdo=Sdo({
                0x6040: Cell(0x0F), 0x6041: Cell(0x27, 0.26), 0x6064: Cell(100, 0.26),
            }), tpdo={3: SimpleNamespace(wait_for_reception=Mock())})
        a = linked_app()
        nodes = {"Actuator A": delayed_node(32), "Actuator B": delayed_node(33)}
        a.nodes = nodes
        a.node = nodes["Actuator A"]
        del a._disable_nodes
        a._prepare_profile_position = Mock()
        a._read_actual_position_inc = Mock(side_effect=lambda *args, **kwargs: (time.sleep(0.26), 100)[1])
        worker = None
        status_reads = 0
        def delayed_status(node, **kwargs):
            nonlocal worker, status_reads
            status_reads += 1
            if worker is None and status_reads == 3:
                a.stop_pending = True
                a.cancel_generation += 1
                a.test_stop_requested.set()
                worker = threading.Thread(target=a._stop_runner, args=("STOP",))
                worker.start()
            time.sleep(0.26)
            return 0x27
        a._read_statusword = Mock(side_effect=delayed_status)
        link = {"enabled": True, "invert_b": False,
                "offsets": a.linked_zero_offsets_inc, "tolerance_um": 20}
        results = []
        def transport(node, value):
            node.sdo[0x6040].raw = value
            node.sdo[0x6041].value = 0x40 if value == 0 else 0x27
        with patch("xeryon_control.application.commands.set_controlword", side_effect=transport), \
             patch("xeryon_control.application.stop.verify_disabled", wraps=verify_disabled) as verified, \
             patch.object(a, "_stop_and_disable_worker", wraps=a._stop_and_disable_worker) as stopped:
            a._command_runner("linked feedback STOP", lambda: results.append(a._run_linked_move(
                {"Actuator A": 1000, "Actuator B": 1000}, {"position_window_inc": 50}, 1, link,
                starts_inc={"Actuator A": 100, "Actuator B": 100})), 0)
            worker.join(3)
        self.assertFalse(worker.is_alive())
        stopped.assert_called_once()
        self.assertEqual(verified.call_count, 2)
        self.assertEqual(a._read_statusword.call_count, 4)
        self.assertEqual(a._read_actual_position_inc.call_count, 4)
        a._quick_stop_nodes.assert_not_called()
        self.assertEqual(results[0]["status"], "Stopped")
        self.assertFalse(a.stop_unconfirmed)

    def test_cancelled_motion_cannot_overwrite_stop(self):
        a = app()
        a.test_stop_requested.set()
        with patch('xeryon_control.application.commands.set_controlword') as send:
            a._write_controlword(a.node,0)
            for word in (0x0F,0x1F,0x10F):
                with self.assertRaises(RuntimeError):
                    a._write_controlword(a.node,word)
            self.assertEqual(send.call_count,1)
        with patch('xeryon_control.application.commands.set_target_position') as target:
            with self.assertRaises(RuntimeError):
                a._write_target_position(a.node,500)
            target.assert_not_called()

    def test_repeat_clicks_do_not_queue_stop_threads(self):
        a = app()
        with patch('xeryon_control.application.stop.threading.Thread') as thread:
            a._run_stop_command('STOP')
            a._run_stop_command('STOP')
            self.assertEqual(thread.call_count,1)
        self.assertTrue(a.test_stop_requested.is_set())

    def test_stop_failure_blocks_restart_and_queued_command(self):
        a = app()
        a.stop_unconfirmed = True
        with self.assertRaises(RuntimeError):
            a._reset_motion_cancel()
        run = Mock()
        a.cancel_generation = 2
        a._command_runner('old move',run,1)
        run.assert_not_called()

    def test_homing_cancellation_cannot_reenable_drive(self):
        a = app()
        a.travel_range = Mock()
        a._invalidate_linked_zero = Mock()
        a._require_node = Mock(return_value=a.node)
        a._disable_drive_worker = Mock()
        a.test_stop_requested.set()
        with patch('xeryon_control.application.commands.set_controlword') as send:
            with self.assertRaises(RuntimeError):
                a._home_worker(True,0,10)
            send.assert_not_called()
        a._disable_drive_worker.assert_called_once()

    def test_ui_deadline_survives_blocked_transport(self):
        a = app()
        a.stop_pending = True
        a.cancel_generation = 3
        a._check_stop_deadline(2)
        self.assertFalse(a.stop_unconfirmed)
        a._check_stop_deadline(3)
        self.assertTrue(a.stop_unconfirmed)
        a._show_error.assert_called_once()

    def test_encoder_initialization_jump_cancels_target(self):
        a = app()
        a.inc_per_mm = 1000
        a.travel_range = Mock()
        a.active_target_inc = None
        a.last_position_inc = 0
        a._require_node = Mock(return_value=a.node)
        a._invalidate_linked_zero = Mock()
        a._validate_target = Mock()
        a._read_actual_position_inc = Mock(side_effect=[0,-24748])
        a._prepare_profile_position = Mock()
        a._disable_drive_worker = Mock()
        a._command_profile_position = Mock()
        with self.assertRaisesRegex(RuntimeError,"意外位移"):
            a._move_absolute_worker(-100,{}, {})
        a._command_profile_position.assert_not_called()
        a._disable_drive_worker.assert_called_once()
        a.travel_range.invalidate.assert_not_called()
        self.assertTrue(a.test_stop_requested.is_set())

    def test_stale_ui_coordinate_while_enabled_cancels_before_enable(self):
        a = app()
        a.inc_per_mm = 1000
        a.travel_range = Mock()
        a.active_target_inc = None
        a.last_position_inc = 0
        a._require_node = Mock(return_value=a.node)
        a._invalidate_linked_zero = Mock()
        a._validate_target = Mock()
        a._read_actual_position_inc = Mock(return_value=-24748)
        a._read_statusword = Mock(return_value=0x27)
        a._prepare_profile_position = Mock()
        a._disable_drive_worker = Mock()
        with self.assertRaisesRegex(RuntimeError,"意外位移"):
            a._move_absolute_worker(-100,{}, {})
        a._prepare_profile_position.assert_not_called()

    def test_disabled_manual_adjustment_uses_live_start_without_moving_zero(self):
        a = app()
        a.inc_per_mm = 1000
        a.travel_range = Mock()
        a.active_target_inc = None
        a.last_position_inc = 15000
        a._require_node = Mock(return_value=a.node)
        a._invalidate_linked_zero = Mock()
        a._validate_target = Mock()
        a._read_statusword = Mock(return_value=0x1250)
        a._read_actual_position_inc = Mock(return_value=15200)
        a._prepare_profile_position = Mock()
        a._start_motion_trace = Mock()
        a._command_profile_position = Mock()
        a._move_absolute_worker(16000, {"velocity": 5, "acceleration": 50}, {})
        a._read_statusword.assert_called_once_with(a.node, fresh=True)
        a._prepare_profile_position.assert_called_once_with(a.node, {"velocity": 5, "acceleration": 50}, 15200)
        a._command_profile_position.assert_called_once_with(a.node, 16000)
        a.travel_range.invalidate.assert_not_called()
        self.assertFalse(a.test_stop_requested.is_set())

    def test_small_enable_settling_keeps_absolute_target(self):
        a = app()
        a.inc_per_mm = 1000
        a.travel_range = Mock()
        a.active_target_inc = None
        a.last_position_inc = 80
        a._require_node = Mock(return_value=a.node)
        a._invalidate_linked_zero = Mock()
        a._validate_target = Mock()
        a._read_actual_position_inc = Mock(side_effect=[80, 70])
        a._prepare_profile_position = Mock()
        a._start_motion_trace = Mock()
        a._command_profile_position = Mock()
        a._move_absolute_worker(1000, {"velocity": 5, "acceleration": 50}, {})
        a._command_profile_position.assert_called_once_with(a.node, 1000)
        self.assertEqual(a.active_start_inc, 70)
        self.assertFalse(a.test_stop_requested.is_set())
        a.travel_range.invalidate.assert_not_called()

    def test_manual_motion_blocks_unrelated_commands_but_allows_disconnect(self):
        a = app()
        a.command_busy = False
        a.busy_var = Mock()
        run = Mock()
        with self.assertRaisesRegex(RuntimeError, "运动进行中"):
            a._run_command("Enable drive", run)
        run.assert_not_called()
        with patch('xeryon_control.application.commands.threading.Thread') as thread:
            a._run_command("Disconnect", run)
            thread.assert_called_once()

    def test_failed_advanced_move_uses_verified_disable(self):
        a = app()
        a.inc_per_mm = 1000
        a.travel_range = Mock()
        a.active_target_inc = None
        a.last_position_inc = 100
        a._require_node = Mock(return_value=a.node)
        a._invalidate_linked_zero = Mock()
        a._validate_target = Mock()
        a._read_actual_position_inc = Mock(return_value=100)
        a._prepare_profile_position = Mock()
        a._start_motion_trace = Mock()
        a._command_profile_position = Mock(side_effect=RuntimeError("lost link"))
        a._stop_and_disable_worker = Mock()
        with self.assertRaisesRegex(RuntimeError, "lost link"):
            a._move_absolute_worker(200, {"velocity": 5, "acceleration": 50}, {})
        a._stop_and_disable_worker.assert_called_once()

    def test_failed_enable_disables_already_prepared_axes(self):
        a = app()
        a._linked_snapshot = Mock(return_value={"enabled": False})
        a._require_node = Mock(return_value=a.node)
        a._prepare_profile_position = Mock(side_effect=RuntimeError("enable failed"))
        a._disable_drive_worker = Mock()
        with self.assertRaisesRegex(RuntimeError, "enable failed"):
            a._enable_drive_worker({})
        a._disable_drive_worker.assert_called_once()


if __name__ == '__main__':
    unittest.main()
