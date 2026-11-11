"""Fault-path regressions for motion, connection and device safety; no hardware."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from control_gui import XeryonControlGui as Gui
from common import utils
from common.parameters import NodeOperationMode
from repeatability import run_repeatability
from travel_range import TravelRange


def var(value):
    return NS(get=lambda: value)


def make_app():
    app = Gui.__new__(Gui)
    app.inc_per_mm = 1000.0
    app.active_axis = "Actuator A"
    app.linked_mode_enabled = False
    app.invert_b_enabled = False
    app.default_limit_enabled = True
    app.travel_range = TravelRange()
    app.travel_ranges = {}
    app.node_identities = {}
    app.position_panel = NS(user_zero_um=0.0, user_zero_set=False)
    app.linked_zero_offsets_inc = {}
    app.io_lock = threading.RLock()
    app.controlword_lock = threading.Lock()
    app.test_stop_requested = threading.Event()
    app.stop_pending = False
    app.stop_verified = False
    app.close_in_progress = False
    app.close_stop_requested = False
    app.close_cleanup_started = False
    app.close_notice = None
    app.busy_var = var("Idle")
    app.stop_unconfirmed = False
    app.cancel_generation = 0
    app.command_busy = False
    app.active_target_inc = None
    app.active_start_inc = None
    app.runtime_position_window_inc = 2
    app._post = Mock()
    app._log = Mock()
    app._record_motion_event = Mock()
    app._show_error = Mock()
    app.after = Mock()
    return app


def linked_app():
    app = make_app()
    app.linked_mode_enabled = True
    app.linked_zero_offsets_inc = {"Actuator A": 0, "Actuator B": 0}
    app.travel_ranges = {}
    for axis in ("Actuator A", "Actuator B"):
        travel = TravelRange()
        travel.confirm(0, 50, axis)
        app.travel_ranges[axis] = travel
    app.nodes = {
        "Actuator A": NS(id=32, tpdo={3: NS(wait_for_reception=Mock())}),
        "Actuator B": NS(id=33, tpdo={3: NS(wait_for_reception=Mock())}),
    }
    app.node = app.nodes["Actuator A"]
    app._prepare_profile_position = Mock()
    app._command_profile_positions = Mock()
    app._disable_nodes = Mock()
    app._quick_stop_nodes = Mock()
    return app


class HardeningTests(unittest.TestCase):
    def test_canable_preferred_over_bluetooth_and_refresh_keeps_explicit_choice(self):
        ports = [NS(device="COM9", vid=None, pid=None),
                 NS(device="COM4", vid=0xAD50, pid=0x60C4)]
        with patch("control_gui.list_ports.comports", return_value=ports):
            names, preferred = Gui._serial_port_choices()
            self.assertCountEqual(names, ["COM4", "COM9"])
            self.assertEqual(preferred, "COM4")
            app = make_app()
            app.node = None
            app.channel_combos = []
            app.channel_var = Mock()
            app.channel_var.get.return_value = "COM9"
            app.refresh_ports()
            app.channel_var.set.assert_not_called()
            app.channel_var.get.return_value = "COM3"
            app.refresh_ports()
            app.channel_var.set.assert_called_once_with("COM4")

    def test_default_limit_requires_coordinate_reference(self):
        app = make_app()
        with self.assertRaisesRegex(ValueError, "置零"):
            app._validate_target(100000, {"enabled": False})

    def test_linked_relative_always_validates_each_axis(self):
        app = linked_app()
        app._read_actual_position_inc = Mock(return_value=0)
        app._validate_target = Mock()
        app._run_linked_move = Mock(return_value={"status": "OK"})
        link = {"enabled": True, "invert_b": False, "offsets": app.linked_zero_offsets_inc, "tolerance_um": 20}
        app._move_linked_relative_worker(100, {"velocity": 5, "acceleration": 50}, {"enabled": False}, link)
        self.assertEqual(app._validate_target.call_count, 2)

    def test_linked_default_limit_uses_logical_coordinates(self):
        app = linked_app()
        app._validate_target(1000, {"enabled": False})
        with self.assertRaisesRegex(ValueError, "默认限位"):
            app._validate_target(-20000, {"enabled": False})

    def test_linked_drive_fault_stops_immediately(self):
        app = linked_app()
        app._read_actual_position_inc = Mock(return_value=100)
        app._read_statusword = Mock(return_value=0x408)
        link = {"enabled": True, "invert_b": False, "offsets": app.linked_zero_offsets_inc, "tolerance_um": 20}
        result = app._run_linked_move({"Actuator A": 100, "Actuator B": 100}, {"position_window_inc": 2}, 1, link,
                                      starts_inc={"Actuator A": 0, "Actuator B": 0})
        self.assertNotEqual(result["status"], "OK")
        app._quick_stop_nodes.assert_called_once()

    def test_linked_final_live_mismatch_cannot_return_ok(self):
        app = linked_app()
        app._read_actual_position_inc = lambda node, fresh=False, **kwargs: 0 if fresh else 100
        app._read_statusword = Mock(return_value=0x427)
        link = {"enabled": True, "invert_b": False, "offsets": app.linked_zero_offsets_inc, "tolerance_um": 20}
        result = app._run_linked_move({"Actuator A": 100, "Actuator B": 100}, {"position_window_inc": 2}, 1, link,
                                      starts_inc={"Actuator A": 0, "Actuator B": 0})
        self.assertNotEqual(result["status"], "OK")

    def test_unexpected_displacement_stops_both_axes_and_preserves_pair_reference(self):
        app = linked_app()
        app.travel_range = Mock()
        app._disable_drive_worker = Mock()
        with self.assertRaises(RuntimeError):
            app._check_position_reference(1000, 20000)
        self.assertEqual(app.linked_zero_offsets_inc, {"Actuator A": 0, "Actuator B": 0})
        app._disable_drive_worker.assert_called_once()
        app.travel_range.invalidate.assert_not_called()

    def test_reconnect_verifies_disable_before_clearing_unknown_stop(self):
        app = make_app()
        app.stop_unconfirmed = True
        app._disable_nodes = Mock()
        app.nodes = {}
        app.node = None
        sdo = type("Sdo", (dict,), {"RESPONSE_TIMEOUT": 1.0, "MAX_RETRIES": 1, "PAUSE_BEFORE_SEND": 0.0})(
            {0x6041: NS(raw=0x27), 0x1802: {1: NS(raw=0x380 + 32), 3: NS(raw=0)}})
        node = NS(id=33, sdo=sdo, nmt=NS(state="OPERATIONAL", add_heartbeat_callback=Mock()), setup_402_state_machine=Mock(),
                  tpdo_pointers={}, tpdo_values={}, rpdo_pointers={})
        network = NS(add_node=Mock(), disconnect=Mock())
        eds_path = str(Path("eds/xeryon_xla_5_eds.eds").resolve())
        config = dict(encoder="1MU", interface="slcan", channel="FAKE", bitrate=125000,
                      node_id=33, eds_path=eds_path, node_b_enabled=False,
                      reset_on_connect=False, boot_timeout=1)
        with patch("control_gui.setup_network", return_value=(network, eds_path)), \
             patch("control_gui.BaseNode402", return_value=node):
            app._connect_worker(config)
        app._disable_nodes.assert_called_once_with([node])
        self.assertFalse(app.stop_unconfirmed)

    def test_test_speed_cannot_raise_cap_or_quantize_to_zero(self):
        app = make_app()
        app._prepare_profile_position = Mock()
        with self.assertRaisesRegex(ValueError, "最大速度"):
            app._run_test_move(NS(id=33), "Actuator A", 1, 1000000, 10,
                               {"max_velocity": 100, "limits": {"enabled": False}}, 1)
        app._prepare_profile_position.assert_not_called()

    def test_dual_reconnect_preserves_disable_requirement_for_every_axis(self):
        app = make_app()
        app.stop_unconfirmed = True
        app.nodes = {}
        app.node = None
        app._configure_connected_node = Mock()
        nodes = [NS(id=32), NS(id=33)]
        network = NS(add_node=Mock(), disconnect=Mock())
        eds_path = str(Path("eds/xeryon_xla_5_eds.eds").resolve())
        config = dict(encoder="1MU", interface="slcan", channel="FAKE", bitrate=125000,
                      node_id=32, node_b_enabled=True, node_b_id=33, eds_path=eds_path,
                      reset_on_connect=False, boot_timeout=1)
        with patch("control_gui.setup_network", return_value=(network, eds_path)), \
             patch("control_gui.BaseNode402", side_effect=nodes):
            app._connect_worker(config)
        self.assertEqual(app._configure_connected_node.call_count, 2)
        for call in app._configure_connected_node.call_args_list:
            self.assertTrue(call.args[1]["require_disable_verification"])
        self.assertFalse(app.stop_unconfirmed)

    def test_single_axis_final_mismatch_cannot_return_ok(self):
        app = make_app()
        app._validate_target = Mock()
        app._prepare_profile_position = Mock()
        app._command_profile_position = Mock()
        app._disable_drive_worker = Mock()
        app._read_actual_position_inc = lambda node, fresh=False, **kwargs: 0 if fresh else 100
        app._read_statusword = Mock(return_value=0x427)
        profile = {"max_velocity": 100, "position_window_inc": 2, "limits": {"enabled": False}}
        result = app._run_test_move(NS(id=33), "Actuator A", 1, 5, 10, profile, 1)
        self.assertNotEqual(result["status"], "OK")

    def test_linked_function_test_enforces_speed_and_target_limits(self):
        app = linked_app()
        app.pair_tolerance_var = var("20")
        app._read_actual_position_inc = Mock(return_value=0)
        app._run_linked_move = Mock()
        config = dict(speeds=[100], distances=[100000], settle_time=0, step_timeout=1,
                      return_after=False,
                      profile={"max_velocity": 50, "limits": {"enabled": False}})
        with self.assertRaisesRegex(ValueError, "最大速度"):
            app._run_linked_actuator_test_worker(config, app._linked_snapshot())
        app._run_linked_move.assert_not_called()
        config["speeds"] = [5]
        with self.assertRaisesRegex(ValueError, "默认限位"):
            app._run_linked_actuator_test_worker(config, app._linked_snapshot())
        app._run_linked_move.assert_not_called()

    def test_profile_snapshot_does_not_change_active_position_window(self):
        app = make_app()
        app.runtime_position_window_inc = 2
        names = ("velocity_var", "acceleration_var", "max_velocity_var", "max_acceleration_var",
                 "quick_stop_decel_var", "position_window_var", "position_window_time_var",
                 "following_error_window_var", "following_error_timeout_var")
        values = (5, 50, 300, 500, 500, 20, 200, 20, 1000)
        for name, value in zip(names, values):
            setattr(app, name, var(str(value)))
        app._profile_snapshot()
        self.assertEqual(app.runtime_position_window_inc, 2)

    def test_standard_cycle_rejects_profile_above_configured_cap(self):
        app = make_app()
        app.position_panel.user_zero_set = True
        app.position_panel.user_zero_um = 0.0
        app.travel_range.confirm(30000, 50, "Actuator A")
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app._require_node = Mock(return_value=app.node)
        app._apply_limits_worker = Mock()
        app._disable_drive_worker = Mock()
        app._read_actual_position_inc = Mock(side_effect=[50, 10050, 13000])
        config = dict(start=50.0, switch=10050.0, end=13000.0,
                      fast_speed=500.0, slow_speed=5.0, fast_acceleration=500.0, timeout=10,
                      disable_after_retreat=True,
                      limits={"enabled": False, "min": 50.0, "max": 13000.0},
                      profile={"max_velocity": 300, "max_velocity_inc": 300,
                               "max_acceleration": 500, "max_acceleration_inc": 500,
                               "acceleration": 50, "quick_stop_decel": 500,
                               "position_window_inc": 5})
        with self.assertRaisesRegex(ValueError, "最大速度"):
            app._run_standard_cycle_worker(config, retreat=False)
        app._apply_limits_worker.assert_not_called()
        config["fast_speed"] = 50.0
        config["fast_acceleration"] = 2000.0
        with self.assertRaisesRegex(ValueError, "最大加速度"):
            app._run_standard_cycle_worker(config, retreat=False)
        app._apply_limits_worker.assert_not_called()
        config["fast_acceleration"] = 500.0
        app._read_actual_position_inc = Mock(side_effect=[50, 10050])
        app._run_test_move = Mock(return_value={"status": "OK"})
        app._run_standard_cycle_worker(config, retreat=False)
        self.assertEqual(app._run_test_move.call_count, 2)
        targets = [call.kwargs["distance_um"] for call in app._run_test_move.call_args_list]
        self.assertEqual(targets, [10000.0, 2950.0])
        app._disable_drive_worker = Mock()
        config["slow_speed"] = 0.1
        with self.assertRaisesRegex(ValueError, "0 计数"):
            app._run_standard_cycle_worker(config, retreat=False)
        app._disable_drive_worker.assert_called_once()

    def test_resume_from_11000_uses_user_zero_and_only_slow_absolute_target(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app.position_panel.user_zero_set = True
        app.position_panel.user_zero_um = 8000
        app.travel_range.confirm(38000, 50, "Actuator A")
        app._read_actual_position_inc = Mock(return_value=19000)
        app._run_test_move = Mock(return_value={"status": "OK"})
        config = dict(start=50, switch=10050, end=13000, fast_speed=50, slow_speed=5,
                      fast_acceleration=500, timeout=900, limits=dict(enabled=True, min=8050, max=21000),
                      profile=dict(max_velocity=300, max_velocity_inc=300, max_acceleration=500,
                                   max_acceleration_inc=500, acceleration=50, quick_stop_decel=500,
                                   position_window_inc=5))
        app._run_standard_cycle_worker_impl(config, False)
        call = app._run_test_move.call_args.kwargs
        self.assertEqual((call["absolute_target_inc"], call["speed_um_s"], call["distance_um"]), (21000, 5, 2000))
        call["before_setpoint"]()
        app._read_actual_position_inc.return_value = 18000
        with self.assertRaisesRegex(RuntimeError, "跨越分段"):
            call["before_setpoint"]()
        app._read_actual_position_inc.return_value = 21000
        app._run_test_move.reset_mock()
        app._run_standard_cycle_worker_impl(config, False)
        app._run_test_move.assert_not_called()

    def test_reference_review_checks_identity_and_preserves_original_zero(self):
        app = make_app()
        app.nodes = {"Actuator A": NS(id=33)}
        app.reference_review = {"Actuator A": 19000}
        app.reconnect_reason = ""
        app.node_identities = {33: (1, 2, 3, 44)}
        app.position_panel.user_zero_um = 8000
        app._safe_start = lambda label, worker: worker()
        with patch("control_gui.lss_read_identity_via_sdo", return_value=(1, 2, 3, 44)), \
             patch("control_gui.verify_disabled", side_effect=[
                 {"position_inc": 19200, "statusword": 0x40},
                 {"position_inc": 19001, "statusword": 0x40},
             ]):
            with self.assertRaisesRegex(RuntimeError, "零点仍待复核"):
                app.review_reference()
            self.assertEqual(app.reconnect_reason, "")
            self.assertEqual(app.reference_review, {"Actuator A": 19000})
            app.review_reference()
        self.assertIsNone(app.reference_review)
        self.assertEqual(app.position_panel.user_zero_um, 8000)
        app.reference_review = {"Actuator A": 19000}
        with patch("control_gui.lss_read_identity_via_sdo", return_value=(1, 2, 3, 45)):
            with self.assertRaisesRegex(RuntimeError, "身份"):
                app.review_reference()
        with self.assertRaisesRegex(RuntimeError, "重新标定"):
            app._reset_motion_cancel()

    def test_requested_and_actual_mode_confirmation_are_separate(self):
        node = NS(id=33, sdo={"Mode of operation": NS(raw=0), 0x6061: NS(raw=0)})
        utils.set_node_operation_mode(node, NodeOperationMode.PROFILE_POSITION, timeout=0.01)
        with self.assertRaisesRegex(TimeoutError, "actual.*reports 0"):
            utils.wait_for_node_operation_mode(node, NodeOperationMode.PROFILE_POSITION, timeout=0.01)
        node.sdo[0x6061].raw = NodeOperationMode.PROFILE_POSITION.value
        utils.wait_for_node_operation_mode(node, NodeOperationMode.PROFILE_POSITION, timeout=0.01)
        class RejectedMode:
            @property
            def raw(self):
                return NodeOperationMode.HOMING.value
            @raw.setter
            def raw(self, value):
                pass
        node.sdo["Mode of operation"] = RejectedMode()
        with self.assertRaisesRegex(TimeoutError, "requested.*reports 6"):
            utils.set_node_operation_mode(node, NodeOperationMode.PROFILE_POSITION, timeout=0.01)

    def test_profile_position_confirms_actual_mode_after_enable(self):
        app = make_app()
        app._initialize_enable_hold = Mock(return_value=0)
        node = NS(id=33, sdo={
            "Mode of operation": NS(raw=0), 0x6061: NS(raw=0),
            "Position window": NS(raw=0), "Position window time": NS(raw=0),
        })
        order = []
        app._write_profile = Mock(side_effect=lambda *args: order.append("profile"))
        app._read_actual_position_inc = Mock(return_value=0)
        def enable_changes_actual_mode(node, controlword, mask, expected):
            order.append(controlword)
            if controlword == 0x0F:
                node.sdo[0x6061].raw = NodeOperationMode.PROFILE_POSITION.value
            node.sdo["Statusword"] = NS(raw=expected)
            return expected
        app._motion_state = Mock(side_effect=enable_changes_actual_mode)
        with patch("control_gui.wait_for_node_operation_mode",
                   side_effect=lambda n, m, **kwargs: utils.wait_for_node_operation_mode(n, m, timeout=0.01, **kwargs)):
            app._prepare_profile_position(node, {"position_window_inc": 5, "position_window_time": 200})
        self.assertEqual(order, ["profile", 0x00, 0x06, 0x07, 0x0F])

    def test_wrong_actual_mode_after_enable_blocks_target(self):
        app = make_app()
        app._initialize_enable_hold = Mock(return_value=0)
        node = NS(id=33, sdo={
            "Mode of operation": NS(raw=0), 0x6061: NS(raw=0),
            "Position window": NS(raw=0), "Position window time": NS(raw=0),
        })
        app.node = node
        app.last_position_inc = 0
        app._require_node = Mock(return_value=node)
        app._validate_target = Mock()
        app._read_actual_position_inc = Mock(return_value=0)
        app._write_profile = Mock()
        app._command_profile_position = Mock()
        app._stop_and_disable_worker = Mock()
        def enable_selects_wrong_mode(node, controlword, mask, expected):
            if controlword == 0x0F:
                node.sdo[0x6061].raw = NodeOperationMode.HOMING.value
            return expected
        app._motion_state = Mock(side_effect=enable_selects_wrong_mode)
        with patch("control_gui.wait_for_node_operation_mode",
                   side_effect=lambda n, m, **kwargs: utils.wait_for_node_operation_mode(n, m, timeout=0.01, **kwargs)):
            with self.assertRaisesRegex(TimeoutError, "actual.*reports 6"):
                app._move_absolute_worker(1000, {"position_window_inc": 5, "position_window_time": 200}, {})
        app._command_profile_position.assert_not_called()
        app._stop_and_disable_worker.assert_called_once()

    def test_example_disables_when_actual_mode_confirmation_fails(self):
        from mode_profile_position import position_loop
        node = NS(id=33, sdo={"Position window": NS(raw=0), "Position window time": NS(raw=0)})
        with patch("mode_profile_position.set_node_state"), \
             patch("mode_profile_position.set_node_operation_mode"), \
             patch("mode_profile_position.wait_for_node_operation_mode", side_effect=TimeoutError("wrong mode")), \
             patch("mode_profile_position.send_position_command") as command, \
             patch("mode_profile_position.verify_disabled") as disabled:
            with self.assertRaisesRegex(TimeoutError, "wrong mode"):
                position_loop(node)
        command.assert_not_called()
        disabled.assert_called_once_with(node)

    def test_example_enable_acknowledgement_failure_still_verifies_disable(self):
        from common.parameters import NodeState
        from mode_profile_position import position_loop
        node = NS(id=33, sdo={"Position window": NS(raw=0), "Position window time": NS(raw=0)})
        def enable_acknowledgement_lost(n, state):
            if state == NodeState.OPERATION_ENABLED:
                raise RuntimeError("enable acknowledgement lost")
        with patch("mode_profile_position.set_node_state", side_effect=enable_acknowledgement_lost), \
             patch("mode_profile_position.set_node_operation_mode"), \
             patch("mode_profile_position.wait_for_node_operation_mode") as confirm, \
             patch("mode_profile_position.send_position_command") as command, \
             patch("mode_profile_position.verify_disabled") as disabled:
            with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                position_loop(node)
        confirm.assert_not_called()
        command.assert_not_called()
        disabled.assert_called_once_with(node)

    def test_example_never_retries_motion_and_interrupt_also_disables(self):
        from mode_profile_position import position_loop
        node = NS(id=33, sdo={"Position window": NS(raw=0), "Position window time": NS(raw=0)})
        for error in (RuntimeError("lost SDO reply"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch("mode_profile_position.set_node_state"), \
                     patch("mode_profile_position.set_node_operation_mode"), \
                     patch("mode_profile_position.wait_for_node_operation_mode"), \
                     patch("mode_profile_position.send_position_command", side_effect=error) as command, \
                     patch("mode_profile_position.verify_disabled") as disabled:
                    with self.assertRaises(type(error)):
                        position_loop(node)
                self.assertEqual(command.call_count, 1)
                disabled.assert_called_once_with(node)

    def test_example_main_verifies_disable_after_homing_error_or_interrupt(self):
        from mode_profile_position import main
        for error in (RuntimeError("mode failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                node = NS(id=33, sdo={"Homing method": NS(raw=0)})
                network = NS(check=Mock(), add_node=Mock(), disconnect=Mock())
                with patch("mode_profile_position.setup_network", return_value=(network, "memory.eds")), \
                     patch("mode_profile_position.BaseNode402", return_value=node), \
                     patch("mode_profile_position.configure_node"), \
                     patch("mode_profile_position.position_loop") as position, \
                     patch("mode_profile_position.time.sleep"), \
                     patch("mode_profile_position.verify_disabled") as main_disabled, \
                     patch.object(utils, "set_node_state"), \
                     patch.object(utils, "set_node_operation_mode"), \
                     patch.object(utils, "wait_for_node_operation_mode", side_effect=error), \
                     patch.object(utils, "set_controlword"), \
                     patch.object(utils, "verify_disabled") as homing_disabled:
                    main()
                homing_disabled.assert_called_once_with(node)
                main_disabled.assert_called_once_with(node)
                position.assert_not_called()
                network.disconnect.assert_called_once()

    def test_homing_wrong_actual_mode_never_sets_start_bit(self):
        app = make_app()
        node = NS(id=33, sdo={"Home offset": NS(raw=0), "Homing method": NS(raw=0)})
        app._require_node = Mock(return_value=node)
        app._motion_state = Mock()
        app._write_controlword = Mock()
        app._disable_drive_worker = Mock()
        with patch("control_gui.set_node_operation_mode"), \
             patch("control_gui.wait_for_node_operation_mode", side_effect=TimeoutError("wrong mode")):
            with self.assertRaisesRegex(TimeoutError, "wrong mode"):
                app._home_worker(True, 0, 1)
        app._write_controlword.assert_not_called()
        app._disable_drive_worker.assert_called_once()

    def test_second_axis_mode_failure_blocks_all_targets(self):
        app = linked_app()
        app._read_actual_position_inc = Mock(return_value=0)
        app._prepare_profile_position = Mock(side_effect=[None, TimeoutError("wrong mode")])
        link = {"enabled": True, "invert_b": False,
                "offsets": app.linked_zero_offsets_inc, "tolerance_um": 20}
        result = app._run_linked_move(
            {"Actuator A": 1000, "Actuator B": 1000}, {"position_window_inc": 50}, 1, link,
            starts_inc={"Actuator A": 0, "Actuator B": 0})
        self.assertTrue(result["status"].startswith("Error:"))
        app._command_profile_positions.assert_not_called()
        app._disable_nodes.assert_called_once()

    def test_network_open_failure_closes_and_write_timeout_is_bounded(self):
        bus = NS(send=Mock())
        original_send = bus.send
        network = NS(bus=bus, connect=Mock(), check=Mock(side_effect=RuntimeError("notifier failed")),
                     disconnect=Mock())
        with patch.object(utils, "Network", return_value=network):
            with self.assertRaises(RuntimeError):
                utils.setup_network(eds_relative_path=str(Path("eds/xeryon_xla_5_eds.eds").resolve()))
        network.disconnect.assert_called_once()
        network.check.side_effect = None
        with patch.object(utils, "Network", return_value=network):
            utils.setup_network(eds_relative_path=str(Path("eds/xeryon_xla_5_eds.eds").resolve()))
        network.bus.send("frame")
        original_send.assert_called_with("frame", timeout=0.25)

    def test_repeatability_initial_failure_disables_and_reports(self):
        from examples.tests.test_repeatability import FakeApp
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as folder:
            app = FakeApp()
            app._read_actual_position_inc = Mock(side_effect=RuntimeError("initial read failed"))
            report = run_repeatability(app, app.config(folder) if hasattr(app, "config") else dict(
                cycles=2, span_inc=10, speed_inc_s=5, profile={}, axis="A",
                encoder="1MU", encoder_verified=True, output_dir=folder))
            self.assertTrue(app.disabled)
            self.assertEqual(report["status"], "STOPPED")
            self.assertEqual(report["completed_steps"], 0)

    def test_repeatability_does_not_raise_custom_speed_cap(self):
        from examples.tests.test_repeatability import FakeApp
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as folder:
            app = FakeApp()
            original_move = app._run_test_move
            def checked_move(node, axis, step, speed, distance, profile, timeout, **kwargs):
                if speed > profile["max_velocity"]:
                    return dict(step=step, start_inc=100, target_inc=110, end_inc=100,
                                status="Error: cap exceeded", duration=0)
                return original_move(node, axis, step, speed, distance, profile, timeout, **kwargs)
            app._run_test_move = checked_move
            config = dict(cycles=2, span_inc=10, speed_inc_s=5,
                          profile={"max_velocity": 4, "max_velocity_inc": 4}, axis="A",
                          encoder="1MU", encoder_verified=True, output_dir=folder)
            report = run_repeatability(app, config)
            self.assertEqual(report["status"], "STOPPED")
            self.assertEqual(app.targets, [])

    def test_repeatability_intersects_custom_limits(self):
        from examples.tests.test_repeatability import FakeApp
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as folder:
            app = FakeApp()
            original_move = app._run_test_move
            profiles = []
            def capture(*args, **kwargs):
                profiles.append(args[5])
                return original_move(*args, **kwargs)
            app._run_test_move = capture
            config = dict(cycles=2, span_inc=10, speed_inc_s=5,
                          profile={"max_velocity": 20, "max_velocity_inc": 20,
                                   "limits": {"enabled": True, "min": 98, "max": 102}},
                          axis="A", encoder="1MU", encoder_verified=True, output_dir=folder)
            report = run_repeatability(app, config)
            self.assertEqual(report["status"], "COMPLETED")
            self.assertTrue(all(profile["limits"] == {"enabled": True, "min": 98, "max": 102}
                                for profile in profiles))

    def test_disconnect_exception_still_clears_connection(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app.network = NS(disconnect=Mock(side_effect=RuntimeError("close failed")))
        app._disable_drive_worker = Mock()
        with self.assertRaises(RuntimeError):
            app._disconnect_worker()
        self.assertIsNone(app.node)
        self.assertIsNone(app.network)
        self.assertEqual(app.nodes, {})

    def test_close_uses_stop_and_failure_does_not_destroy(self):
        app = make_app()
        app.close_in_progress = False
        app.nodes = {"Actuator A": NS(id=33)}
        app._cancel_shortcut_queue = Mock()
        app._run_stop_command = Mock()
        app._show_close_notice = Mock(side_effect=lambda: setattr(app, "close_notice", Mock()))
        app._center_notice = Mock()
        app.destroy = Mock()
        app._on_close()
        app._run_stop_command.assert_called_once()
        app.destroy.assert_not_called()
        app._on_close()
        app._center_notice.assert_called_once_with(app.close_notice)
        app._run_stop_command.assert_called_once()
        app.destroy.assert_not_called()
        app._show_close_notice.assert_called_once()
        app._show_error.assert_not_called()

    def test_close_destroys_only_after_background_cleanup_finishes(self):
        app = make_app()
        app.close_in_progress = True
        app.close_stop_requested = True
        app.stop_verified = True
        app.close_deadline = float("inf")
        app.node = NS(id=33)
        app.nodes = {"A": app.node}
        app.network = Mock()
        network = app.network
        app._disable_drive_worker = Mock()
        app.stop_unconfirmed = False
        app._run_command = Mock()
        app.destroy = Mock()
        app._finish_close()
        label, cleanup = app._run_command.call_args.args
        self.assertEqual(label, "Disconnect")
        cleanup()
        network.disconnect.assert_called_once()
        app._disable_drive_worker.assert_not_called()
        app.destroy.assert_not_called()
        app.after.assert_called_with(100, app._finish_close)
        app.close_cleanup_started = True
        app._finish_close()
        app.destroy.assert_called_once()

    def test_hung_transport_offers_exit_and_keeps_motion_blocked(self):
        app = make_app()
        app.nodes = {"A": NS(id=33)}
        app.stop_pending = app.close_in_progress = app.close_stop_requested = True
        app.close_deadline = 0
        app._show_close_notice = Mock()
        app.destroy = Mock()
        app._finish_close()
        app._show_close_notice.assert_called_once()
        app.destroy.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "不能提交"):
            app._run_command("Move absolute", Mock())

    def test_busy_disconnect_waits_then_runs_cleanup(self):
        app = make_app()
        app.disconnect_in_progress = False
        app.nodes = {"Actuator A": NS(id=33)}
        app.stop_pending = True
        app.command_busy = True
        app._cancel_shortcut_queue = Mock()
        app._run_stop_command = Mock()
        app._run_command = Mock()
        app.disconnect_device()
        self.assertTrue(app.disconnect_in_progress)
        app.stop_pending = False
        app.command_busy = False
        app._finish_disconnect()
        app._run_command.assert_called_once_with("Disconnect", app._disconnect_worker)

    def test_invalid_device_readback_retains_safe_values(self):
        app = make_app()
        values = {
            "Position Actual Value": -29348, 0x6063: -30000,
            "Profile velocity": 100, "Profile acceleration": 100, "Max profile velocity": 1000,
            "Max acceleration": 1000, "Quick stop deceleration": 3023700,
            "Position window": 0xFFFFFFFF, "Position window time": 0,
            "Following error window": 0xFFFFFFFF, "Following error time out": 0,
            "Software position limit": {1: NS(raw=0), 2: NS(raw=0)},
        }
        node = NS(sdo={key: NS(raw=value) for key, value in values.items()})
        node.sdo["Software position limit"] = values["Software position limit"]
        app._require_node = Mock(return_value=node)
        app._read_device_worker()
        app._post.assert_any_call("log", "位置读回：0x6064 实际位置 = -29348 计数，0x6063 内部位置 = -30000")
        payload = app._post.call_args.args[1]
        self.assertNotIn("quick_stop_decel", payload)
        self.assertNotIn("position_window", payload)

    def test_low_speed_quantization_is_rejected(self):
        app = make_app()
        app.inc_per_mm = 100.0
        names = ("velocity_var", "acceleration_var", "max_velocity_var", "max_acceleration_var",
                 "quick_stop_decel_var", "position_window_var", "position_window_time_var",
                 "following_error_window_var", "following_error_timeout_var")
        values = (3, 50, 300, 500, 500, 5, 200, 20, 1000)
        for name, value in zip(names, values):
            setattr(app, name, var(str(value)))
        with self.assertRaisesRegex(ValueError, "0 计数"):
            app._profile_snapshot()

    def test_idle_link_loss_requests_disable_and_marks_unhealthy(self):
        app = make_app()
        app.status_busy = True
        node = NS(tpdo={3: NS(wait_for_reception=Mock())})
        app.node = node
        app.nodes = {"Actuator A": node}
        app._read_actual_position_inc = Mock(side_effect=RuntimeError("lost link"))
        app._disable_nodes = Mock()
        app._status_worker()
        app._disable_nodes.assert_called_once()
        self.assertTrue(app.test_stop_requested.is_set())
        self.assertTrue(any(call.args == ("connection_health", False) for call in app._post.call_args_list))

    def test_lss_mismatch_restores_waiting_state(self):
        lss = NS(WAITING_STATE=4, send_switch_state_global=Mock(),
                 send_switch_state_selective=Mock(return_value=True), inquire_node_id=Mock(return_value=31))
        network = NS(lss=lss, add_node=Mock())
        with patch.object(utils, "BaseNode402", return_value=NS()), \
             patch.object(utils, "lss_read_identity_via_sdo", return_value=(1, 2, 3, 4)):
            result = utils.lss_configure_single_node_id(network, 32, 33)
        self.assertEqual(result, 3)
        self.assertEqual(lss.send_switch_state_global.call_count, 2)
        lss.send_switch_state_global.assert_called_with(4)

    def test_failed_homing_never_reenables(self):
        node = NS(id=33, sdo={"Homing method": NS(raw=0)})
        with patch.object(utils, "set_node_state"), \
             patch.object(utils, "set_node_operation_mode"), \
             patch.object(utils, "wait_for_node_operation_mode"), \
             patch.object(utils, "verify_disabled"), \
             patch.object(utils, "wait_for_statusword_flags", side_effect=RuntimeError("failed")), \
             patch.object(utils, "set_controlword") as control:
            with self.assertRaises(RuntimeError):
                utils.homing(node, timeout=0.01)
        control.assert_called_with(node, 0x00)
    def test_lss_scan_error_restores_waiting_state(self):
        lss = NS(WAITING_STATE=4, send_switch_state_global=Mock(),
                 fast_scan=Mock(return_value=(True, (1, 2, 3, 4))),
                 configure_node_id=Mock(side_effect=RuntimeError("configure failed")))
        network = NS(lss=lss)
        result = utils.lss_scan_and_configure_nodes(network, 32)
        self.assertEqual(result, [])
        self.assertGreaterEqual(lss.send_switch_state_global.call_count, 2)
        lss.send_switch_state_global.assert_called_with(4)

    def test_homing_start_send_failure_also_disables(self):
        node = NS(id=33, sdo={"Homing method": NS(raw=0)})
        with patch.object(utils, "set_node_state"), \
             patch.object(utils, "set_node_operation_mode"), \
             patch.object(utils, "wait_for_node_operation_mode"), \
             patch.object(utils, "verify_disabled"), \
             patch.object(utils, "set_controlword", side_effect=[RuntimeError("send failed"), None]) as control:
            with self.assertRaises(RuntimeError):
                utils.homing(node, timeout=0.01)
        self.assertEqual(control.call_args_list[0].args[1], 0x0F | 0x10)
        self.assertEqual(control.call_args_list[-1].args[1], 0x00)
    def test_homing_keyboard_interrupt_also_disables(self):
        node = NS(id=33, sdo={"Homing method": NS(raw=0)})
        with patch.object(utils, "set_node_state"), \
             patch.object(utils, "set_node_operation_mode"), \
             patch.object(utils, "wait_for_node_operation_mode"), \
             patch.object(utils, "verify_disabled"), \
             patch.object(utils, "wait_for_statusword_flags", side_effect=KeyboardInterrupt), \
             patch.object(utils, "set_controlword") as control:
            with self.assertRaises(KeyboardInterrupt):
                utils.homing(node, timeout=0.01)
        self.assertEqual(control.call_args_list[-1].args[1], 0x00)
    def test_linked_final_pair_mismatch_cannot_return_ok(self):
        app = linked_app()
        app.runtime_position_window_inc = 50
        app._read_actual_position_inc = lambda node, fresh=False, **kwargs: (
            950 if node.id == 32 else 1050)
        app._read_statusword = Mock(return_value=0x427)
        app.pair_tolerance_var = var("20")
        link = {"enabled": True, "invert_b": False,
                "offsets": {"Actuator A": 0, "Actuator B": 1000}, "tolerance_um": 20}
        result = app._run_linked_move({"Actuator A": 1000, "Actuator B": 1000},
                                      {"position_window_inc": 50}, 1, link,
                                      starts_inc={"Actuator A": 0, "Actuator B": 1000})
        self.assertNotEqual(result["status"], "OK")
        app._disable_nodes.assert_called_once()

    def test_homing_clear_bit4_failure_also_disables(self):
        node = NS(id=33, sdo={"Homing method": NS(raw=0)})
        with patch.object(utils, "set_node_state"), \
             patch.object(utils, "set_node_operation_mode"), \
             patch.object(utils, "wait_for_node_operation_mode"), \
             patch.object(utils, "verify_disabled"), \
             patch.object(utils, "wait_for_statusword_flags", return_value=None), \
             patch.object(utils, "set_controlword", side_effect=[None, RuntimeError("clear failed"), None]) as control:
            with self.assertRaises(RuntimeError):
                utils.homing(node, timeout=0.01)
        self.assertEqual(control.call_args_list[0].args[1], 0x0F | 0x10)
        self.assertEqual(control.call_args_list[1].args[1], 0x0F)
        self.assertEqual(control.call_args_list[-1].args[1], 0x00)
    def test_old_command_cannot_clear_stop_generation(self):
        app = make_app()
        app.nodes = {"Actuator A": NS(id=33)}
        app.node = app.nodes["Actuator A"]
        def old_command():
            app._reset_motion_cancel()
            app.cancel_generation += 1
            app.test_stop_requested.clear()
            app._write_controlword(app.node, 0x0F)
        with patch("control_gui.set_controlword") as send:
            app._command_runner("old move", old_command, 0)
            send.assert_not_called()

    def test_standard_cycle_intersects_narrower_custom_limits(self):
        app = make_app()
        app.position_panel.default_limit_active = lambda: True
        app.position_panel.user_zero_set = True
        app.travel_range.confirm(30000, 50, "Actuator A")
        app.limit_enabled_var = var(True)
        app.min_limit_var = var("1000")
        app.max_limit_var = var("12000")
        names = ("workflow_start_var", "workflow_switch_var", "workflow_end_var",
                 "workflow_fast_speed_var", "workflow_slow_speed_var", "workflow_fast_acceleration_var",
                 "workflow_travel_limit_var", "workflow_timeout_var",
                 "velocity_var", "acceleration_var",
                 "max_velocity_var", "max_acceleration_var", "quick_stop_decel_var",
                 "position_window_var", "position_window_time_var", "following_error_window_var",
                 "following_error_timeout_var")
        values = (50, 10050, 13000, 50, 5, 500, 15000, 900,
                  5, 50, 300, 500, 500, 5, 200, 20, 1000)
        for name, value in zip(names, values):
            setattr(app, name, var(str(value)))
        config = app._workflow_snapshot()
        self.assertEqual(config["limits"], {"enabled": True, "min": 1000, "max": 12000})

    def test_standard_cycle_requires_user_zero(self):
        app = make_app()
        app._disable_drive_worker = Mock()
        with self.assertRaisesRegex(ValueError, "置零"):
            app._run_standard_cycle_worker({}, retreat=False)

    def test_disconnect_disable_failure_is_not_silent(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app.network = NS(disconnect=Mock())
        app._disable_drive_worker = Mock(side_effect=RuntimeError("disable failed"))
        with self.assertRaisesRegex(RuntimeError, "disable failed"):
            app._disconnect_worker()
        self.assertIsNone(app.node)
        self.assertIsNone(app.network)

    def test_absolute_move_applies_enabled_limits_before_motion(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app._require_node = Mock(return_value=app.node)
        app._validate_target = Mock()
        app._apply_limits_worker = Mock()
        app._read_actual_position_inc = Mock(return_value=100)
        app._prepare_profile_position = Mock()
        app._start_motion_trace = Mock()
        app._command_profile_position = Mock()
        app._invalidate_linked_zero = Mock()
        app.last_position_inc = 100
        limits = {"enabled": True, "min": 0, "max": 13000}
        app._move_absolute_worker(1000, {"velocity": 5, "acceleration": 50}, limits)
        app._apply_limits_worker.assert_called_once_with(limits)

    def test_linked_move_applies_enabled_limits_before_motion(self):
        app = linked_app()
        app._apply_linked_limits_worker = Mock()
        app._read_actual_position_inc = Mock(return_value=100)
        app._read_statusword = Mock(return_value=0x427)
        link = {"enabled": True, "invert_b": False, "offsets": app.linked_zero_offsets_inc, "tolerance_um": 20}
        limits = {"enabled": True, "min": 0, "max": 13000}
        profile = {"position_window_inc": 2, "limits": limits}
        app._run_linked_move({"Actuator A": 100, "Actuator B": 100}, profile, 1, link,
                             starts_inc={"Actuator A": 0, "Actuator B": 0})
        app._apply_linked_limits_worker.assert_called_once_with(limits, link)
    def test_linked_target_must_fit_each_axis_mechanical_mapping(self):
        app = linked_app()
        app._read_actual_position_inc = Mock(side_effect=lambda node, **kwargs: 29500 if node.id == 32 else 0)
        link = {"enabled": True, "invert_b": False,
                "offsets": {"Actuator A": 29500, "Actuator B": 0}, "tolerance_um": 20}
        with self.assertRaisesRegex(ValueError, "行程范围"):
            app._run_linked_move({"Actuator A": 30500, "Actuator B": 1000},
                                 {"position_window_inc": 2}, 1, link,
                                 starts_inc={"Actuator A": 29500, "Actuator B": 0})

    def test_communication_failure_suspends_linked_zero_and_mappings(self):
        app = linked_app()
        app.status_busy = True
        app._read_actual_position_inc = Mock(side_effect=RuntimeError("lost link"))
        app._disable_nodes = Mock()
        app.reference_positions = {"Actuator A": 87, "Actuator B": 92}
        app._status_worker()
        self.assertEqual(app.reference_review, app.reference_positions)
        self.assertEqual(len(app.travel_ranges), 2)
        self.assertEqual(len(app.linked_zero_offsets_inc), 2)
        with self.assertRaisesRegex(RuntimeError, "待复核"):
            app._reset_motion_cancel()

    def test_test_move_passes_original_start_into_prepare(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app._validate_target = Mock()
        app._prepare_profile_position = Mock()
        app._command_profile_position = Mock()
        app._disable_drive_worker = Mock(return_value={33: {"statusword": 0x40, "position_inc": 100}})
        app._read_actual_position_inc = Mock(return_value=100)
        app._read_statusword = Mock(return_value=0x427)
        profile = {"max_velocity": 100, "position_window_inc": 2, "limits": {"enabled": False}}
        app._run_test_move(app.node, "Actuator A", 1, 5, 0, profile, 1)
        expected_profile = dict(profile, velocity=5, velocity_inc=5)
        app._prepare_profile_position.assert_called_once_with(app.node, expected_profile, 100)

    def test_disable_position_drift_prevents_test_success(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app._validate_target = Mock()
        app._prepare_profile_position = Mock()
        app._command_profile_position = Mock()
        app._read_actual_position_inc = Mock(return_value=110)
        app._read_statusword = Mock(return_value=0x427)
        app._disable_drive_worker = Mock(return_value={33: {"statusword": 0x40, "position_inc": 140}})
        profile = {"max_velocity": 100, "position_window_inc": 2, "limits": {"enabled": False}}
        result = app._run_test_move(app.node, "Actuator A", 1, 5, 10, profile, 1)
        self.assertNotEqual(result["status"], "OK")

    def test_disable_position_drift_prevents_shortcut_completion(self):
        app = make_app()
        app.status_busy = True
        node = NS(id=33, tpdo={3: NS(wait_for_reception=Mock())})
        app.node = node
        app.nodes = {"Actuator A": node}
        app.active_target_inc = 110
        app.active_start_inc = 0
        app.active_target_time = 0
        app.active_target_deadline = 100
        app.active_best_remaining_inc = 110
        app.active_last_progress_time = 0
        app.shortcut_active = {"generation": 7}
        app._read_actual_position_inc = Mock(return_value=110)
        app._read_statusword = Mock(return_value=0x427)
        app._read_mode = Mock(return_value="PROFILE_POSITION")
        app._disable_drive_worker = Mock(return_value={33: {"statusword": 0x40, "position_inc": 140}})
        app._status_worker()
        self.assertFalse(any(call.args == ("shortcut_done", 7) for call in app._post.call_args_list))
        self.assertTrue(any(call.args[0] == "shortcut_failed" for call in app._post.call_args_list))
    def test_successful_disable_clears_unconfirmed_stop(self):
        app = make_app()
        app.node = NS(id=33)
        app.nodes = {"Actuator A": app.node}
        app.stop_unconfirmed = True
        app._disable_nodes = Mock(return_value={33: {"statusword": 0x40, "position_inc": 100}})
        result = app._disable_drive_worker()
        self.assertFalse(app.stop_unconfirmed)
        self.assertEqual(result[33]["position_inc"], 100)


if __name__ == "__main__":
    unittest.main()
