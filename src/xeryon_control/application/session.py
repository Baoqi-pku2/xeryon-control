"""Controller operations and UI-independent session state."""
from __future__ import annotations

import logging
import csv
import os
import queue
import time
from dataclasses import replace
from math import ceil, floor, isfinite
from pathlib import Path
from typing import Callable, Optional

from canopen import BaseNode402, Network
from canopen.sdo.exceptions import SdoAbortedError
from serial.tools import list_ports

from xeryon_control.hardware.parameters import EncoderRes, HomingMethod, NodeOperationMode
from xeryon_control.reporting.traces import append_event, new_trace_path, results_folder
from xeryon_control.application.stop import StopCoordinator
from xeryon_control.application.commands import CommandDispatcher
from xeryon_control.application.feedback import (
    FeedbackMonitoring, MOTION_PROGRESS_EPSILON_UM, MOTION_STALL_TIMEOUT_SECONDS,
    poll_feedback, start_poll_if_due,
)
from xeryon_control.application.linked_motion import LinkedMotion
from xeryon_control.application.motion import PanelMotion, StartConfirmation, StartupDisplacement
from xeryon_control.application.calibration import sample_stationary_encoder
from xeryon_control.hardware.drive import command_profile_positions, check_halted_profile_mode, operation_disabled
from xeryon_control.application.repeatability import run_repeatability
from xeryon_control.application.position import PositionModel
from xeryon_control.application.fields import Field
from xeryon_control.domain.shortcut_presets import SHORTCUT_PRESETS
from xeryon_control.domain.travel import DEFAULT_WORK_MAX_UM, DEFAULT_WORK_MIN_UM, TravelRange
from xeryon_control.domain.coordinates import AxisOrigins, from_counts, to_counts, to_encoder, to_user
from xeryon_control.domain.workflow import next_workflow_stage
from xeryon_control.hardware.stop import verify_disabled
from xeryon_control.hardware.canopen_io import (
    BIT,
    bound_can_write_timeout,
    lss_read_identity_via_sdo,
    set_node_operation_mode,
    setup_network,
    wait_for_node_operation_mode,
)
from xeryon_control.config import (
    CAN_BITRATE,
    CAN_CHANNEL,
    CAN_INTERFACE,
    DEFAULT_SDO_TIMEOUT,
    EDS_PATH,
    INC_PER_MM,
    NODE_ID,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("canopen").setLevel(logging.WARNING)


POLL_INTERVAL_MS = 100
STATUS_POLL_REST_SECONDS = 0.1
UI_EVENTS_PER_TICK = 100
UI_EVENT_BUDGET_SECONDS = 0.01
LOG_MAX_LINES = 2000
SETPOINT_ACK_TIMEOUT_SECONDS = 2.0
TPDO3_INHIBIT_TIME_100US = 200
POSITION_REFERENCE_TOLERANCE_UM = 20.0
MANUAL_TIMEOUT_FACTOR = 2.0
MANUAL_TIMEOUT_MARGIN_SECONDS = 30.0
PROFILE_VELOCITY_LIMIT_UM_S = 400_000.0
PROFILE_ACCELERATION_LIMIT_UM_S2 = 3_000_000.0

class ControllerSession(CommandDispatcher, StopCoordinator, FeedbackMonitoring, LinkedMotion):
    """Device session and presentation state; independent of a window system."""

    def __init__(self) -> None:

        self.view = None
        self.available_axes = ["Actuator A"]
        self.origins = AxisOrigins()
        self.travel_range = TravelRange()
        self.inc_per_mm = float(INC_PER_MM)
        self.test_results = []
        self.motion_trace_path = None
        self.controller_trace_next_time = 0.0
        self.controller_trace_unsupported = set()

        self.network = None
        self.node: Optional[BaseNode402] = None
        self.nodes: dict[str, BaseNode402] = {}
        self._init_command_dispatch()
        self._init_stop_control()
        self.status_busy = False
        self.active_target_inc: Optional[int] = None
        self.active_start_inc: Optional[int] = None
        self.active_target_time = 0.0
        self.active_target_deadline = 0.0
        self.active_best_remaining_inc: Optional[int] = None
        self.active_last_progress_time = 0.0
        self.last_position_inc: Optional[int] = None
        self.runtime_position_window_inc = self._um_to_inc(20.0)
        self.linked_zero_offsets_inc: dict[str, int] = {}
        self.reference_review = None
        self.reconnect_reason = ""
        self.reference_positions = {}
        self.node_identities = {}
        self.travel_ranges: dict[str, TravelRange] = {}
        self.shortcut_active: Optional[dict[str, object]] = None
        self.shortcut_pending: list[dict[str, object]] = []
        self.shortcut_selected: list[int] = []
        self.shortcut_generation = 0
        self.shortcut_wait_deadline: Optional[float] = None
        self.shortcut_feedback_wait_started: Optional[float] = None
        self.shortcut_wait_timer = None
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.active_axis = "Actuator A"
        self.linked_mode_enabled = False
        self.invert_b_enabled = False
        self.default_limit_enabled = True
        self.pair_tolerance_um = 20.0
        self.last_statusword: Optional[int] = None
        self.disconnect_in_progress = False
        self.close_in_progress = False
        self.close_cleanup_started = False
        self.close_stop_requested = False
        self.close_notice = None
        self.start_confirmation = None

        self._build_variables()
        self.active_axis_var.subscribe(lambda *args: setattr(self, "active_axis", self.active_axis_var.get()))
        self.linked_mode_var.subscribe(lambda *args: setattr(self, "linked_mode_enabled", bool(self.linked_mode_var.get())))
        self.invert_b_var.subscribe(lambda *args: setattr(self, "invert_b_enabled", bool(self.invert_b_var.get())))
        self.default_limit_var.subscribe(lambda *args: setattr(self, "default_limit_enabled", bool(self.default_limit_var.get())))
        self.pair_tolerance_var.subscribe(lambda *args: self._sync_pair_tolerance())
        self.position_panel = PositionModel(self)

    def _build_variables(self) -> None:
        self.serial_ports, default_channel = self._serial_port_choices()

        self.interface_var = Field(value=str(CAN_INTERFACE))
        self.channel_var = Field(value=default_channel)
        self.bitrate_var = Field(value=str(int(CAN_BITRATE)))
        self.node_id_var = Field(value=str(NODE_ID))
        self.node_b_enabled_var = Field(value=False)
        self.node_b_id_var = Field(value="33")
        self.active_axis_var = Field(value="Actuator A")
        self.linked_mode_var = Field(value=False)
        self.invert_b_var = Field(value=False)
        self.pair_tolerance_var = Field(value="20.0")
        self.pair_zero_status_var = Field(value="Not set")
        self.position_a_var = Field(value="-")
        self.position_b_var = Field(value="-")
        self.pair_error_var = Field(value="-")
        self.eds_path_var = Field(value=EDS_PATH)
        self.reset_on_connect_var = Field(value=False)
        self.boot_timeout_var = Field(value="8")
        self.probe_limit_var = Field(value="64")

        self.velocity_var = Field(value="5.0")
        self.acceleration_var = Field(value="50.0")
        self.max_velocity_var = Field(value="300.0")
        self.max_acceleration_var = Field(value="500.0")
        self.quick_stop_decel_var = Field(value="500.0")
        self.position_window_var = Field(value="5.0")
        self.position_window_time_var = Field(value="200")
        self.following_error_window_var = Field(value="20.0")
        self.following_error_timeout_var = Field(value="1000")

        self.target_position_var = Field(value="0.0")
        self.relative_distance_var = Field(value="10.0")
        self.jog_step_var = Field(value="1.0")
        self.home_direction_var = Field(value="Positive index")
        self.home_offset_var = Field(value="0.0")
        self.home_timeout_var = Field(value="30")

        self.limit_enabled_var = Field(value=False)
        self.default_limit_var = Field(value=True)
        self.min_limit_var = Field(value="-29000.0")
        self.max_limit_var = Field(value="29000.0")

        self.connected_var = Field(value="Disconnected")
        self.busy_var = Field(value="Idle")
        self.position_var = Field(value="-")
        self.user_position_var = Field(value="-")
        self.target_status_var = Field(value="-")
        self.target_start_var = Field(value="-")
        self.target_moved_var = Field(value="-")
        self.target_remaining_var = Field(value="-")
        self.target_progress_var = Field(value="-")
        self.target_progress_bar_var = Field(value=0.0)
        self.shortcut_status_var = Field(value="可多选步骤，再点「开始运动」")
        self.shortcut_dwell_var = Field(value="30")
        self.test_speeds_var = Field(value="5,10,20")
        self.test_distances_var = Field(value="50,100,200")
        self.test_return_var = Field(value=False)
        self.test_settle_var = Field(value="0.2")
        self.test_timeout_var = Field(value="60")
        self.workflow_start_var = Field(value="50")
        self.workflow_switch_var = Field(value="10050")
        self.workflow_end_var = Field(value="13000")
        self.workflow_fast_speed_var = Field(value="50.0")
        self.workflow_slow_speed_var = Field(value="5.0")
        self.workflow_fast_acceleration_var = Field(value="500.0")
        self.workflow_travel_limit_var = Field(value="15000.0")
        self.workflow_timeout_var = Field(value="900")
        self.workflow_preview_var = Field(value="等待有效位置反馈")
        self.workflow_status_var = Field(value="Ready")
        self.statusword_var = Field(value="-")
        self.state_var = Field(value="-")
        self.mode_var = Field(value="-")
        self.encoder_var = Field(value="1MU")
        self.repeat_cycles_var = Field(value="3")
        self.repeat_span_var = Field(value="10")
        self.repeat_speed_var = Field(value="5")
        self.repeat_status_var = Field(value="待测试；结果为编码器重复性，不代表外部绝对精度")
        self.last_report_dir = None

    @staticmethod
    def _serial_port_choices() -> tuple[list[str], str]:
        ports = list_ports.comports()
        names = [port.device for port in sorted(ports, key=lambda p: p.device.casefold())]
        canable = [port.device for port in ports if (port.vid, port.pid) == (0xAD50, 0x60C4)]
        configured = str(CAN_CHANNEL)
        if len(canable) == 1:
            return names, canable[0]
        usb = [port.device for port in ports if port.vid is not None]
        if configured not in names and len(usb) == 1:
            return names, usb[0]
        return names, configured

    def refresh_ports(self) -> None:
        self.serial_ports, preferred = self._serial_port_choices()
        if self.node is None and self.serial_ports and self.channel_var.get() not in self.serial_ports:
            self.channel_var.set(preferred)
        self._log(
            f"Serial ports: {', '.join(self.serial_ports)}"
            if self.serial_ports
            else "No serial ports found."
        )

    # ------------------------------------------------------------------
    # Command wrappers
    # ------------------------------------------------------------------
    def connect_device(self) -> None:
        connection = self._connection_snapshot()
        self._safe_start("Connect", lambda: self._connect_worker(connection))

    def export_test_results(self) -> None:
        if not self.test_results:
            self._show_error("还没有测试结果。")
            return
        self.view.choose_csv(self._write_results_csv)

    def _write_results_csv(self, path):
        if path:
            try:
                fields = sorted(set().union(*(r.keys() for r in self.test_results)) - {"samples"})
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(self.test_results)
            except OSError as exc:
                self._show_error(str(exc))

    def open_report_folder(self) -> None:
        if self.last_report_dir:
            os.startfile(self.last_report_dir)

    def start_repeatability(self) -> None:
        if self.command_busy:
            return
        try:
            self._require_node()
            if self.linked_mode_var.get():
                raise ValueError("重复定位测试请关闭双轴联动，分别测试每根推进器。")
            profile = self._profile_snapshot()
            profile["limits"] = self._limits_snapshot()
            config = {
                "cycles": self._parse_positive_int(self.repeat_cycles_var, "cycles"),
                "span_inc": self._parse_positive_int(self.repeat_span_var, "span counts"),
                "speed_inc_s": self._parse_positive_int(self.repeat_speed_var, "speed counts/s"),
                "axis": self.active_axis_var.get(), "profile": profile,
                "encoder": self.encoder_var.get(), "encoder_verified": False,
                "output_dir": str(results_folder()),
            }
            node = self.node
            generation = self.cancel_generation

            def start_confirmed():
                if (self.node is not node or self.cancel_generation != generation
                        or self.active_axis_var.get() != config["axis"] or self.linked_mode_var.get()
                        or self.close_in_progress or self.disconnect_in_progress):
                    raise RuntimeError("设备状态已变化，请重新发起测试。")
                self._reset_motion_cancel()
                self.repeat_status_var.set("正在测试；点击顶部 STOP 可中止")
                def run():
                    self._post("repeat_report", run_repeatability(self, config))
                self._safe_start("重复定位", run)

            self.view.confirm("空载往返测试", "请确认推杆空载、位于中部，两端留有余量。\n"
                              "从当前位置向正方向短程往返，结束后关闭驱动。\n"
                              "以编码器计数评估重复性。绝对精度需外部量具。", start_confirmed)
        except Exception as exc:
            self._show_error(str(exc))

    def disconnect_device(self) -> None:
        if self.disconnect_in_progress or self.close_in_progress:
            return
        self.disconnect_in_progress = True
        self._cancel_shortcut_queue("快捷流程已断开")
        self.test_stop_requested.set()
        if self.nodes:
            self._run_stop_command("Disconnect")
        self.schedule(100, self._finish_disconnect)

    def _finish_disconnect(self) -> None:
        if not self.disconnect_in_progress:
            return
        if self.stop_pending or self.command_busy or self.__dict__.get("status_busy", False):
            self.schedule(100, self._finish_disconnect)
            return
        self.disconnect_in_progress = False
        if self.stop_unconfirmed:
            self._show_error("停止状态未确认，不能安全断开。请关闭驱动器电源后再断开。")
            return
        try:
            self._run_command("Disconnect", self._disconnect_worker)
        except Exception as exc:
            self._show_error(str(exc))

    def read_device_settings(self) -> None:
        self._safe_start("Read device", self._read_device_worker)

    def probe_node_ids(self) -> None:
        probe = self._probe_snapshot()
        self._safe_start("Probe node IDs", lambda: self._probe_node_ids_worker(probe))

    def probe_bitrates(self) -> None:
        probe = self._probe_snapshot()
        self._safe_start("Probe bitrates", lambda: self._probe_bitrates_worker(probe))

    def apply_motion_profile(self) -> None:
        profile = self._profile_snapshot()
        self._safe_start("Apply profile", lambda: self._apply_profile_worker(profile))

    def move_to_target(self) -> None:
        try:
            self._check_reference_ready()
            panel = self.position_panel
            if not panel.is_fresh():
                raise ValueError("请等待有效的位置反馈。")
            linked = self._linked_snapshot(require_zero=True)
            if not linked["enabled"] and not panel.user_zero_set:
                raise ValueError("请先确认本轴收回端点零点。")
            target_um = self._parse_float(panel.user_target, "目标位置")
            if not linked["enabled"]:
                target_um = to_encoder(target_um, panel.user_zero_um)
            profile = self._profile_snapshot()
            limits = self._limits_snapshot()
            self._validate_target(target_um, limits)
        except (ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
            return

        def start() -> None:
            self._reset_motion_cancel()
            if bool(linked["enabled"]):
                self._move_linked_absolute_worker(target_um, profile, limits, linked)
            else:
                self._move_absolute_worker(target_um, profile, limits)

        self._safe_start("Move absolute", start)

    def start_position_panel_move(self) -> None:
        """Capture UI values on the UI thread; only Start can submit this move."""
        panel = self.position_panel
        selected = list(self.shortcut_selected)
        self._log("收到“开始运动”点击；快捷选择：" + (" → ".join(map(str, selected)) if selected else "无"))
        self.target_status_var.set("正在检查所选快捷流程" if selected else "正在检查运动参数")
        try:
            if (self.start_confirmation is not None or self.command_busy or self.active_target_inc is not None
                    or self.shortcut_active is not None or self.shortcut_pending):
                raise ValueError("运动或快捷流程正在进行，请等待完成或先停止。")
            if self.shortcut_selected:
                self.start_selected_shortcuts()
                return
            node = self._require_node()
            if self.linked_mode_var.get():
                raise ValueError("实时位置页用于单轴控制，请先在高级控制中关闭双轴联动。")
            if not panel.is_fresh():
                raise ValueError("尚无有效的实时位置，请连接设备并等待编码器反馈。")
            target = self._parse_float(panel.target, "目标位置")
            self.travel_range.validate(target, self._um_to_inc(target), self._inc_to_um(1), self.active_axis_var.get())
            speed = self._parse_positive_float(panel.speed, "速度")
            if not 2 <= speed <= 500:
                raise ValueError("控制台速度须在 2–500 µm/s 范围内，且不能超过高级设置中的最大速度。")
            profile = self._profile_snapshot(velocity_override=speed)
            limits = self._limits_snapshot()
            self._validate_target(target, limits)
            self._reset_motion_cancel()
            self._submit_panel_move("开始运动", target, profile, limits, node)
        except Exception as exc:
            self.target_status_var.set("未启动，请查看运行日志")
            self._log(f"开始运动未执行：{exc}")
            self._show_error(str(exc))

    def _shortcut_snapshot(self, number: int) -> dict[str, object]:
        try:
            preset_number, name, user_target, speed = SHORTCUT_PRESETS[number - 1]
            if preset_number != number:
                raise IndexError
        except (IndexError, TypeError) as exc:
            raise ValueError("未知快捷指令。") from exc

        node = self._require_node()
        panel = self.position_panel
        if self.linked_mode_var.get():
            raise ValueError("快捷指令仅用于单轴控制，请先关闭双轴联动。")
        if not panel.is_fresh():
            raise ValueError("尚无有效的实时位置，请连接设备并等待编码器反馈。")
        if not panel.user_zero_set or not self.travel_range.confirmed:
            raise ValueError("请先将推杆置于收回端点，并点击“实物当前位置 = 0”。")
        if not 2 <= speed <= 500:
            raise ValueError("序列速度须在 2–500 µm/s 范围内。")

        axis = self._active_axis_name()
        if number == 5:
            user_target = self._parse_float(self.workflow_start_var, "工作起点")
        target = to_encoder(user_target, panel.user_zero_um)
        try:
            self.travel_range.validate(target, self._um_to_inc(target), self._inc_to_um(1), axis)
        except ValueError as exc:
            raise ValueError(
                f"快捷 {number} {name}：用户目标 {user_target:g} µm，"
                f"收回端零点为编码器 {panel.user_zero_um:.2f} µm。{exc}"
            ) from exc
        limits = self._limits_snapshot()
        self._validate_target(target, limits)
        dwell = self._parse_nonnegative_float(self.shortcut_dwell_var, "步骤间停留秒数")
        if dwell > 86400:
            raise ValueError("步骤间停留不能超过 86400 秒。")
        return {
            "number": number,
            "name": name,
            "user_target": user_target,
            "target": target,
            "speed": speed,
            "profile": self._profile_snapshot(velocity_override=speed),
            "limits": limits,
            "axis": axis,
            "node": node,
            "generation": 0,
            "dwell_seconds": dwell,
        }

    def toggle_shortcut(self, number: int) -> None:
        try:
            if number not in {preset[0] for preset in SHORTCUT_PRESETS}:
                raise ValueError("未知快捷指令。")
            if (self.command_busy or self.active_target_inc is not None
                    or self.shortcut_active is not None or self.shortcut_pending
                    or self.stop_pending or self.stop_unconfirmed):
                raise ValueError("运动或快捷流程执行期间不能修改选择。")
            if number in self.shortcut_selected:
                self.shortcut_selected.remove(number)
            else:
                self.shortcut_selected.append(number)
            self._update_shortcut_status()
            self.position_panel.redraw()
        except Exception as exc:
            self._show_error(str(exc))

    def start_selected_shortcuts(self) -> None:
        try:
            if not self.shortcut_selected:
                raise ValueError("请先选择至少一个快捷指令。")
            if (self.command_busy or self.active_target_inc is not None
                    or self.shortcut_active is not None or self.shortcut_pending):
                raise ValueError("其他运动或快捷流程正在执行，不能启动所选指令。")
            steps = [self._shortcut_snapshot(number) for number in self.shortcut_selected]
            self.shortcut_generation += 1
            for step in steps:
                step["generation"] = self.shortcut_generation
            self.shortcut_active = steps[0]
            self.shortcut_pending = steps[1:]
            try:
                self._start_shortcut_step(self.shortcut_active)
            except Exception:
                self._cancel_shortcut_queue("快捷流程启动失败")
                raise
            self._log("已启动所选快捷流程：" + " → ".join(str(step["number"]) for step in steps))
        except Exception as exc:
            self.target_status_var.set("未启动，请查看运行日志")
            self._log(f"快捷流程未启动：{exc}")
            self._show_error(str(exc))

    def start_shortcut(self, number: int) -> None:
        created_sequence = False
        try:
            step = self._shortcut_snapshot(number)
            active = self.shortcut_active
            if active is not None:
                step["generation"] = active["generation"]
                self.shortcut_pending.append(step)
                self._log(f"快捷 {number} {step['name']} 已加入队列。")
                self._update_shortcut_status()
                self.position_panel.redraw()
                return

            if self.command_busy or self.active_target_inc is not None:
                raise ValueError("其他运动或命令正在执行，不能启动快捷指令。")
            self.shortcut_generation += 1
            step["generation"] = self.shortcut_generation
            self.shortcut_active = step
            self.shortcut_pending.clear()
            created_sequence = True
            self._start_shortcut_step(step)
        except Exception as exc:
            if created_sequence:
                self._cancel_shortcut_queue("快捷流程启动失败")
            self._show_error(str(exc))

    def _start_shortcut_step(self, step: dict[str, object]) -> None:
        number = int(step["number"])
        name = str(step["name"])
        self.position_panel.user_target.set(f"{float(step['user_target']):.2f}")
        self.position_panel.speed.set(f"{float(step['speed']):g}")
        self.velocity_var.set(f"{float(step['speed']):g}")
        self._reset_motion_cancel()

        if self._submit_panel_move(f"快捷 {number} {name}", float(step["target"]),
                                   step["profile"], step["limits"], step["node"], step) is False:
            raise RuntimeError("快捷步骤未提交，序列已取消；请查看运行日志。")
        self._update_shortcut_status()
        self.position_panel.redraw()

    def _submit_panel_move(self, label, target, profile, limits, node, step=None):
        panel = self.position_panel
        motion = PanelMotion(label, target, dict(profile), dict(limits), node,
                             self._active_axis_name() if step is None else step["axis"],
                             panel.user_zero_um, panel.user_zero_set, self._um_to_inc(panel.position),
                             self.travel_range.command_bounds,
                             self.cancel_generation, step)
        return self._dispatch_panel_move(motion)

    def _dispatch_panel_move(self, motion, approved_start=None):
        def start():
            try:
                if motion.generation != self.cancel_generation:
                    raise RuntimeError("启动请求已取消。")
                if motion.step is not None and (self.shortcut_active is not motion.step or
                                                motion.step["generation"] != self.shortcut_generation):
                    raise RuntimeError("快捷流程已取消。")
                if self.test_stop_requested.is_set():
                    return
                if self._require_node() is not motion.node or self._active_axis_name() != motion.axis:
                    raise RuntimeError("活动轴已改变，运动未开始。")
                if approved_start is not None and not operation_disabled(
                        self._read_statusword(motion.node, fresh=True)):
                    raise RuntimeError("等待确认期间驱动状态已改变，继续请求已取消。")
                expected = motion.displayed_inc if approved_start is None else approved_start
                # The driver adds derived jerk values to its working profile.
                # Keep the operator's original settings for the confirmation check.
                self._move_absolute_worker(motion.target, dict(motion.profile), motion.limits,
                                           expected_start_inc=expected)
            except StartupDisplacement as exc:
                stopped = exc.stopped.get(motion.node.id) if isinstance(exc.stopped, dict) else None
                if stopped and operation_disabled(stopped["statusword"]):
                    return StartConfirmation(motion, exc.previous_inc, stopped["position_inc"])
                self._post_shortcut_failure(str(exc))
                raise
            except Exception as exc:
                self._post_shortcut_failure(str(exc))
                raise
        return self._safe_start(motion.label, start)

    def _update_shortcut_status(self) -> None:
        active = self.shortcut_active
        if active is None:
            if self.shortcut_selected:
                selected = " → ".join(str(number) for number in self.shortcut_selected)
                self.shortcut_status_var.set(f"执行顺序：{selected}")
            else:
                self.shortcut_status_var.set("可多选步骤，再点「开始运动」")
            return
        deadline = self.__dict__.get("shortcut_wait_deadline")
        if deadline is not None:
            current = f"步骤 {active['number']} 已到位，停留 {max(0, ceil(deadline - time.monotonic()))} 秒后自动继续"
        else:
            current = f"执行第 {active['number']} 步：{active['name']}"
        if self.shortcut_pending:
            waiting = " → ".join(str(step["number"]) for step in self.shortcut_pending)
            current += f" ｜ 待执行：{waiting}"
        self.shortcut_status_var.set(current)

    def _handle_shortcut_done(self, generation: int) -> None:
        active = self.shortcut_active
        if active is None or int(active["generation"]) != generation:
            return
        if self.shortcut_wait_deadline is not None:
            return  # Duplicate completion must not restart or shorten the dwell.
        self._log(f"快捷 {active['number']} {active['name']} 已到位，驱动已关闭。")
        if self.shortcut_pending:
            dwell = float(active.get("dwell_seconds", 30))
            self.shortcut_feedback_wait_started = None
            self.shortcut_wait_deadline = time.monotonic() + dwell
            self._record_motion_event("sequence_dwell_started", step=active["number"], seconds=dwell)
            self._continue_shortcut_after_dwell(generation)
            return

        self.shortcut_active = None
        self.shortcut_selected.clear()
        self.shortcut_generation += 1
        self.shortcut_status_var.set("全部到位")
        self.position_panel.redraw()

    def _continue_shortcut_after_dwell(self, generation: int) -> None:
        self.shortcut_wait_timer = None
        active = self.shortcut_active
        if (active is None or generation != self.shortcut_generation
                or int(active["generation"]) != generation or self.shortcut_wait_deadline is None):
            return
        if (self.test_stop_requested.is_set() or self.stop_pending or self.stop_unconfirmed
                or self.close_in_progress or self.disconnect_in_progress):
            self._cancel_shortcut_queue("快捷流程已取消，停留后不会自动启动。")
            return
        self._update_shortcut_status()
        if time.monotonic() < self.shortcut_wait_deadline or self.command_busy:
            self.shortcut_wait_timer = self.schedule(100, lambda: self._continue_shortcut_after_dwell(generation))
            return
        try:
            if not self.position_panel.is_fresh():
                # A completed command can leave the UI sample stale until the
                # next idle status poll. Stay disabled and allow a bounded
                # refresh; never submit a new target using expired feedback.
                now = time.monotonic()
                started = self.__dict__.get("shortcut_feedback_wait_started")
                if started is None:
                    self.shortcut_feedback_wait_started = now
                    self._log("停留结束，等待新位置反馈后自动继续；尚未发送下一目标。")
                elif now - started >= 2.0:
                    raise RuntimeError("停留结束后 2 秒内未获得新位置反馈，已取消后续步骤。")
                self.shortcut_status_var.set("停留结束，等待位置反馈")
                self.shortcut_wait_timer = self.schedule(100, lambda: self._continue_shortcut_after_dwell(generation))
                return
            if not self.shortcut_pending:
                raise RuntimeError("序列缺少下一步，已取消。")
            self.shortcut_feedback_wait_started = None
            self.shortcut_wait_deadline = None
            self.shortcut_active = self.shortcut_pending.pop(0)
            self._record_motion_event("sequence_dwell_finished", next_step=self.shortcut_active["number"])
            self._start_shortcut_step(self.shortcut_active)
        except Exception as exc:
            self._cancel_shortcut_queue(f"快捷流程中止：{exc}")
            self._show_error(str(exc))

    def _handle_shortcut_failed(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        active = self.shortcut_active
        if active is None or int(active["generation"]) != int(payload["generation"]):
            return
        self._cancel_shortcut_queue(f"快捷流程中止：{payload['reason']}")

    def _cancel_shortcut_queue(self, reason: str = "快捷流程已取消") -> None:
        timer = self.__dict__.get("shortcut_wait_timer")
        if timer is not None:
            self.cancel_timer(timer)
        self.shortcut_wait_timer = None
        self.shortcut_wait_deadline = None
        self.shortcut_feedback_wait_started = None
        self.shortcut_generation += 1
        self.shortcut_active = None
        self.shortcut_pending.clear()
        self.shortcut_selected.clear()
        self.shortcut_status_var.set(reason)
        panel = self.__dict__.get("position_panel")
        if panel is not None:
            panel.redraw()

    def clear_shortcut_queue(self) -> None:
        if self.shortcut_active is not None or self.shortcut_pending:
            self._show_error("快捷流程执行期间不能修改选择；如需退出请使用 STOP。")
            return
        self.shortcut_selected.clear()
        self._update_shortcut_status()
        self._log("已清空快捷指令选择；未发送运动命令。")
        self.position_panel.redraw()

    def _shortcut_generation_token(self) -> Optional[int]:
        active = self.__dict__.get("shortcut_active")
        return int(active["generation"]) if active is not None else None

    def _post_shortcut_failure(self, reason: str) -> None:
        generation = self._shortcut_generation_token()
        if generation is not None:
            self._post("shortcut_failed", {"generation": generation, "reason": reason})

    def set_current_user_zero(self) -> None:
        try:
            panel = self.position_panel
            if panel.editing_locked() or not panel.is_fresh() or self.linked_mode_var.get():
                raise ValueError("请先停止单轴运动，并等待有效位置反馈。")
            node, axis = self._require_node(), self.active_axis_var.get()
            self._safe_start("刷新编码器并置零", lambda: self._capture_user_zero_worker(node, axis))
        except Exception as exc:
            self._show_error(str(exc))

    def refresh_device(self):
        if self.network is None:
            self.refresh_ports()
            return
        axis = self.active_axis_var.get()
        if axis not in self.nodes:
            self._show_error("当前轴尚未连接。")
            return
        node = self._require_node()
        self._safe_start("刷新位置", lambda: self._refresh_position_worker(node, axis))

    def _refresh_position_worker(self, node, axis):
        generation = self.cancel_generation
        if axis != self._active_axis_name() or self.nodes.get(axis) is not node:
            raise RuntimeError("活动轴已变化，请重新刷新。")
        self._reset_motion_cancel()
        self._check_motion_cancelled()
        status = self._read_statusword(node, fresh=True)
        if operation_disabled(status):
            position = self._sample_stationary_position(node)
            status = self._read_statusword(node, fresh=True)
            if not operation_disabled(status):
                raise RuntimeError("驱动关闭状态未确认，刷新已取消。")
        elif status & 0x6F == 0x27:
            position = self._read_actual_position_inc(node, fresh=True)
            status = self._read_statusword(node, fresh=True)
            if status & 0x6F != 0x27:
                raise RuntimeError("读取时驱动状态改变，请重新刷新。")
        else:
            raise RuntimeError(f"当前状态无法刷新位置（0x{status:04X}）。")
        self._check_motion_cancelled()
        self._post("refreshed_position", {
            "axis": axis, "node": node, "generation": generation,
            "sample_time": time.monotonic(), "position_inc": position,
            "position_um": self._inc_to_um(position), "statusword": status,
            "state": str(node.state), "mode": self._read_mode(node),
        })

    def _capture_user_zero_worker(self, node, axis):
        generation = self.cancel_generation
        self._reset_motion_cancel()
        position = self._sample_stationary_position(node)
        self._post("user_zero", {"axis": axis, "node": node, "generation": generation,
                                 "raw_um": self._inc_to_um(position), "sample_time": time.monotonic()})

    def _sample_stationary_position(self, node):
        def enable_and_sample(baseline):
            self._switch_on_for_position_mode(node, baseline)
            self._initialize_enable_hold(node, baseline)
            # Halt stays set throughout sampling; never pulse the new-setpoint bit.
            halt_enabled = 0x0F | BIT(8)
            self._motion_state(node, halt_enabled, 0x6F, 0x27)
            enabled = self._read_actual_position_inc(node, fresh=True)
            check_halted_profile_mode(node, halt_enabled)
            return enabled

        def disable_and_settle():
            stopped = self._disable_drive_worker(only_node=node)[node.id]
            return operation_disabled(stopped["statusword"]), stopped["position_inc"]

        baseline, enabled, position = sample_stationary_encoder(
            read_position=lambda: self._read_actual_position_inc(node, fresh=True),
            is_disabled=lambda: operation_disabled(self._read_statusword(node, fresh=True)),
            enable_and_sample=enable_and_sample,
            is_enabled=lambda: self._read_statusword(node, fresh=True) & 0x6F == 0x27,
            disable_and_settle=disable_and_settle,
            check_cancelled=self._check_motion_cancelled, stop_event=self.test_stop_requested,
        )
        self._post("log", f"节点 {node.id} 编码器采样：开启前 {baseline} → 开启后 {enabled} → 停稳 {position} 计数")
        return position

    def move_to_work_start(self) -> None:
        self.position_panel.user_target.set(self.workflow_start_var.get())
        self.move_to_target()

    def move_relative(self, direction: int) -> None:
        distance_um = self._parse_positive_float(self.relative_distance_var, "relative distance")
        profile = self._profile_snapshot()
        limits = self._limits_snapshot()
        linked = self._linked_snapshot()

        def start() -> None:
            self._reset_motion_cancel()
            if bool(linked["enabled"]):
                self._move_linked_relative_worker(direction * distance_um, profile, limits, linked)
            else:
                self._move_relative_worker(direction * distance_um, profile, limits)

        self._safe_start("Move relative", start)

    def jog(self, direction: int) -> None:
        step_um = self._parse_positive_float(self.jog_step_var, "jog step")
        profile = self._profile_snapshot()
        limits = self._limits_snapshot()
        linked = self._linked_snapshot()

        def start() -> None:
            self._reset_motion_cancel()
            if bool(linked["enabled"]):
                self._move_linked_relative_worker(direction * step_um, profile, limits, linked)
            else:
                self._move_relative_worker(direction * step_um, profile, limits)

        self._safe_start("Jog", start)

    def home_device(self) -> None:
        if self._linked_mode_enabled():
            raise ValueError("Disable A+B linked mode and home each actuator separately.")
        home_config = self._home_snapshot()

        def start() -> None:
            self._reset_motion_cancel()
            self._home_worker(*home_config)

        self._safe_start("Home", start)

    def halt_motion(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Halt")

    def stop_target_motion(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Stop target")

    def quick_stop(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Quick stop")

    def set_linked_zero(self) -> None:
        self._safe_start("Set pair zero", self._set_linked_zero_worker)

    def enable_drive(self) -> None:
        profile = self._profile_snapshot()
        def start():
            self._reset_motion_cancel()
            self._enable_drive_worker(profile)
        self._safe_start("开启驱动", start)

    def disable_drive(self) -> None:
        self.stop_actuator_test()

    def apply_limits(self) -> None:
        limits = self._limits_snapshot()
        self._safe_start("Apply limits", lambda: self._apply_limits_worker(limits))

    def clear_limits(self) -> None:
        self._safe_start("Clear limits", self._clear_limits_worker)

    def run_actuator_test(self) -> None:
        test_config = self._test_snapshot()

        def start() -> None:
            self._reset_motion_cancel()
            self._post("clear_test_results", None)
            self._run_actuator_test_worker(test_config)

        self._safe_start("Run test", start)

    def stop_actuator_test(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("STOP")


    def run_standard_cycle(self, retreat: bool) -> None:
        try:
            config = self._workflow_snapshot()
            self._require_node()
        except (ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
            return

        def start() -> None:
            self._reset_motion_cancel()
            self._run_standard_cycle_worker(config, retreat)

        self._safe_start("Retreat" if retreat else "Approach", start)

    def _update_workflow_preview(self):
        panel = self.position_panel
        try:
            self._check_reference_ready()
            if not panel.is_fresh():
                raise ValueError("等待当前轴位置反馈")
            if self._linked_mode_enabled():
                linked = self._linked_snapshot(require_zero=True)
                if self.pair_error_var.get() == "-":
                    raise ValueError("等待双轴联动坐标反馈")
                positions = [float(self.position_a_var.get()), float(self.position_b_var.get())]
                if max(positions) - min(positions) > linked["tolerance_um"]:
                    raise ValueError("双轴位置差超过容差")
            else:
                if not panel.user_zero_set or not self.travel_range.confirmed:
                    raise ValueError("请确认本轴收回端点零点和行程")
                positions = [to_user(panel.position, panel.user_zero_um)]
            start, switch, end = (self._parse_float(v, name) for v, name in (
                (self.workflow_start_var, "工作起点"), (self.workflow_switch_var, "切换点"),
                (self.workflow_end_var, "终点")))
            window = self._inc_to_um(self._um_to_inc(self._parse_positive_float(self.position_window_var, "到位窗口")))
            current = " / ".join(f"{value:.1f}" for value in positions)
            lines = []
            for label, retreat in (("推进", False), ("退出", True)):
                stage = next_workflow_stage(positions, start, switch, end, window, retreat)
                lines.append(f"{label}：{stage[1]} {current} → {stage[0]:g} µm" if stage else f"{label}：已到位")
            preview = "\n".join(lines)
        except (ValueError, RuntimeError) as exc:
            preview = str(exc)
        if preview != self.workflow_preview_var.get():
            self.workflow_preview_var.set(preview)

    def stop_standard_cycle(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Stop cycle")

    def _on_command_started(self, label):
        self.busy_var.set(label)
        if self.__dict__.get("view"):
            self.view.refresh()

    def _safe_start(self, label: str, func: Callable[[], object]) -> bool:
        try:
            self._run_command(label, func)
            return True
        except Exception as exc:
            self._log(f"{label} not started: {exc}")
            self._show_error(str(exc))
            return False


    def _run_stop_command(self, label: str) -> None:
        self._dismiss_start_confirmation()
        timeout = self._request_stop(label, notify=self.target_status_var.set)
        if timeout is not None:
            generation, seconds = timeout
            self.schedule(int(seconds * 1000), lambda: self._check_stop_deadline(generation))

    def _check_stop_deadline(self, generation):
        if self._stop_timed_out(generation):
            self.target_status_var.set("停止确认超时，请断开驱动电源")
            self._show_error(f"STOP 已请求，但 {self.stop_deadline_seconds:.1f} 秒内未确认停止。请关闭驱动器电源。")


    # ------------------------------------------------------------------
    # CANopen operations
    # ------------------------------------------------------------------
    def _connect_worker(self, connection: dict[str, object]) -> None:
        if self.node is not None:
            raise RuntimeError("Device is already connected.")
        self.inc_per_mm = float(EncoderRes["ENC_RES_" + str(connection.get("encoder", "1MU"))].value)

        interface = str(connection["interface"])
        channel = str(connection["channel"])
        bitrate = int(connection["bitrate"])
        node_id = int(connection["node_id"])
        eds_path = Path(str(connection["eds_path"]))

        if not eds_path.exists():
            raise FileNotFoundError(f"EDS file not found: {eds_path}")

        network = None
        try:
            network, resolved_eds = setup_network(
                interface=interface,
                channel=channel,
                bitrate=bitrate,
                eds_relative_path=str(eds_path),
            )
            node_map: dict[str, BaseNode402] = {}
            node_ids = [("Actuator A", node_id)]
            if bool(connection["node_b_enabled"]):
                node_ids.append(("Actuator B", int(connection["node_b_id"])))

            connection = dict(connection)
            connection["require_disable_verification"] = self.stop_unconfirmed
            for axis_label, axis_node_id in node_ids:
                node = BaseNode402(axis_node_id, resolved_eds)
                network.add_node(node)
                self._post("log", f"{axis_label}: configuring node {axis_node_id}.")
                self._configure_connected_node(node, connection)
                node_map[axis_label] = node
        except Exception as exc:
            if network is not None:
                try:
                    network.disconnect()
                except Exception:
                    pass
            self.network = None
            self.node = None
            self.nodes = {}
            self._post("connected", False)
            raise

        self.reference_positions = {}
        self.reference_review = None
        self.reconnect_reason = ""
        self.network = network
        self.stop_unconfirmed = False
        self.nodes = node_map
        self.node = self.nodes["Actuator A"]
        self.active_target_inc = None
        self.active_start_inc = None
        self.linked_zero_offsets_inc = {}
        self._post("pair_zero_status", "Not set")
        self._post("linked_status", None)
        self._post("axes", list(self.nodes))
        self._post("active_axis", "Actuator A")
        self._post("connected", True)
        self._post("log", f"Connected {', '.join(self.nodes)} on {interface}:{channel}.")

    def _configure_connected_node(self, node: BaseNode402, connection: dict[str, object]) -> None:
        reset_on_connect = bool(connection["reset_on_connect"])
        boot_timeout = float(connection["boot_timeout"])
        # Verify SDO readiness before sending NMT or modifying PDO configuration.
        node.sdo.RESPONSE_TIMEOUT = DEFAULT_SDO_TIMEOUT
        initial_status = int(node.sdo[0x6041].raw)
        self._post("log", f"Node {node.id}: initial status 0x{initial_status:04X}.")
        disabled = ((initial_status & 0x4F) in (0x40, 0x08)
                    or (initial_status & 0x6F) in (0x21, 0x23))
        if bool(connection.get("require_disable_verification")) or not disabled:
            self._post("log", f"Node {node.id}: verifying disabled state before connection setup.")
            self._disable_nodes([node])

        if reset_on_connect:
            try:
                node.nmt.state = "RESET"
                node.nmt.wait_for_bootup(boot_timeout)
                self._post("log", f"Node {node.id}: boot-up received.")
            except Exception as exc:
                self._post(
                    "log",
                    (
                        f"Node {node.id}: boot-up wait failed ({exc}); "
                        "trying direct SDO/PDO setup without reset."
                    ),
                )
        else:
            self._post("log", f"Node {node.id}: skipping NMT reset and boot-up wait.")

        node.nmt.state = "PRE-OPERATIONAL"
        time.sleep(0.1)
        node.sdo.RESPONSE_TIMEOUT = min(DEFAULT_SDO_TIMEOUT, 1.0)
        node.sdo.MAX_RETRIES = 1
        node.sdo.PAUSE_BEFORE_SEND = 0.02
        tpdo3_cob_id = int(node.sdo[0x1802][1].raw)
        node.sdo[0x1802][1].raw = tpdo3_cob_id | BIT(31)
        node.sdo[0x1802][3].raw = TPDO3_INHIBIT_TIME_100US
        node.sdo[0x1802][1].raw = tpdo3_cob_id
        node.setup_402_state_machine()
        node.tpdo_pointers.pop(0x6041, None)
        node.tpdo_values.pop(0x6041, None)
        node.rpdo_pointers.pop(0x6040, None)
        node.nmt.state = "OPERATIONAL"

        try:
            identity = lss_read_identity_via_sdo(node)
        except Exception:
            identity = None
            self._post("log", f"Node {node.id}: 身份读取失败，通信中断后须重新标定。")
        self.node_identities[node.id] = identity
        node.nmt.add_heartbeat_callback(lambda state: self._on_node_boot(node, state))

    def _on_node_boot(self, node, state):
        if state == 0 and any(current is node for current in self.nodes.values()):
            self.reconnect_reason = f"节点 {node.id} 已重启，请断开重连并重新标定。"
            self.test_stop_requested.set()
            self._post("node_boot", node)

    def _check_reference_ready(self):
        if self.__dict__.get("reconnect_reason"):
            raise RuntimeError(self.reconnect_reason)
        if self.__dict__.get("reference_review") is not None:
            raise RuntimeError("通信中断后零点待复核，请点击「复核零点」。")

    def review_reference(self):
        def review():
            anchors = self.reference_review
            if self.reconnect_reason or anchors is None:
                raise RuntimeError(self.reconnect_reason or "没有需要复核的零点。")
            limit = max(2, self._um_to_inc(POSITION_REFERENCE_TOLERANCE_UM))
            for axis, node in self.nodes.items():
                identity = self.node_identities.get(node.id)
                if not identity or not identity[-1] or lss_read_identity_via_sdo(node) != identity:
                    reason = f"{axis} 设备身份无法确认"
                elif axis not in anchors:
                    reason = f"{axis} 缺少故障前的位置记录"
                else:
                    try:
                        result = verify_disabled(node)
                    except Exception:
                        self.stop_unconfirmed = True
                        raise
                    if result["statusword"] & BIT(3):
                        raise RuntimeError(f"{axis} 驱动器故障，零点仍待复核。")
                    if abs(result["position_inc"] - anchors[axis]) <= limit:
                        continue
                    raise RuntimeError(
                        f"{axis} 通信中断前后位置发生变化，零点仍待复核。"
                        f"请核对故障前的参考位置（编码器 {self._inc_to_um(anchors[axis]):.2f} µm）"
                        "后再次复核；若确认编码器基准已丢失，请重连并重新标定。"
                    )
                self.reconnect_reason = reason + "，请断开重连并重新标定。"
                raise RuntimeError(self.reconnect_reason)
            if self.reconnect_reason:
                raise RuntimeError(self.reconnect_reason)
            self.reference_review = None
            self._post("target", "零点已复核，请重新选择运动")
        self._safe_start("复核零点", review)

    def _probe_node_ids_worker(self, probe: dict[str, object]) -> list[int]:
        network = None
        try:
            network = Network()
            network.connect(
                interface=str(probe["interface"]),
                channel=str(probe["channel"]),
                bitrate=int(probe["bitrate"]),
            )
            bound_can_write_timeout(network)
            network.check()
            network.scanner.reset()
            network.scanner.search(limit=int(probe["limit"]))
            time.sleep(1.0)
            nodes = sorted(network.scanner.nodes)

            if nodes:
                self._post("log", f"Probe found node IDs: {', '.join(str(node) for node in nodes)}")
                if len(nodes) == 1:
                    self._post("node_id", str(nodes[0]))
            else:
                self._post("log", "Probe found no CANopen node IDs.")
            return nodes
        finally:
            if network is not None:
                network.disconnect()

    def _probe_bitrates_worker(self, probe: dict[str, object]) -> None:
        bitrates = [125000, 250000, 500000, 1000000, 750000, 100000, 83300, 50000, 20000, 10000]
        found_any = False

        for bitrate in bitrates:
            self._post("log", f"Probing bitrate {bitrate}...")
            try:
                nodes = self._probe_node_ids_worker({**probe, "bitrate": bitrate})
            except Exception as exc:
                self._post("log", f"Bitrate {bitrate}: probe failed ({exc}).")
                time.sleep(0.2)
                continue

            if nodes:
                found_any = True
                self._post(
                    "log",
                    f"Bitrate {bitrate}: found node IDs {', '.join(str(node) for node in nodes)}.",
                )
                self._post("bitrate", str(bitrate))
                if len(nodes) == 1:
                    self._post("node_id", str(nodes[0]))
                break

            time.sleep(0.2)

        if not found_any:
            self._post("log", "No CANopen node IDs found at common bitrates.")

    def _disconnect_worker(self, *, verify_stop: bool = True) -> None:
        disable_error = None
        try:
            if verify_stop and self.node is not None:
                try:
                    self._disable_drive_worker()
                except Exception as exc:
                    disable_error = exc
                    self._post("log", f"Drive disable before disconnect failed: {exc}")
            if self.network is not None:
                self.network.disconnect()
        finally:
            self.reference_review = None
            self.reconnect_reason = ""
            self.reference_positions = {}
            self.node_identities = {}
            self.network = None
            self.node = None
            self.nodes = {}
            self.linked_zero_offsets_inc = {}
            self.active_target_inc = None
            self.active_start_inc = None
            self.last_position_inc = None
            self._post("connected", False)
            self._post("status", None)
            self._post("pair_zero_status", "Not set")
            self._post("linked_status", None)
        if disable_error is not None:
            raise disable_error

    def _read_device_worker(self) -> None:
        node = self._require_node()
        actual = self._read_actual_position_inc(node, fresh=True)
        try:
            internal = str(int(node.sdo[0x6063].raw))
        except (KeyError, SdoAbortedError):
            internal = "设备未提供"
        self._post("log", f"位置读回：0x6064 实际位置 = {actual} 计数，0x6063 内部位置 = {internal}")

        velocity = self._inc_to_um(float(node.sdo["Profile velocity"].raw))
        acceleration = self._inc_to_um(float(node.sdo["Profile acceleration"].raw))
        max_velocity = self._inc_to_um(float(node.sdo["Max profile velocity"].raw))
        max_acceleration = self._inc_to_um(float(node.sdo["Max acceleration"].raw))
        quick_stop_raw = int(node.sdo["Quick stop deceleration"].raw)
        quick_stop_decel = self._inc_to_um(float(quick_stop_raw))
        position_window_raw = int(node.sdo["Position window"].raw)
        position_window = self._inc_to_um(float(position_window_raw))
        position_window_time = int(node.sdo["Position window time"].raw)
        following_error_raw = int(node.sdo["Following error window"].raw)
        following_error_timeout = int(node.sdo["Following error time out"].raw)

        limits = node.sdo["Software position limit"]
        min_limit = self._inc_to_um(float(limits[1].raw))
        max_limit = self._inc_to_um(float(limits[2].raw))

        settings = {
            "velocity": velocity,
            "acceleration": acceleration,
            "max_velocity": max_velocity,
            "max_acceleration": max_acceleration,
            "quick_stop_decel": quick_stop_decel,
            "position_window": position_window,
            "position_window_time": position_window_time,
            "min_limit": min_limit,
            "max_limit": max_limit,
            "limits_enabled": not (limits[1].raw == 0 and limits[2].raw == 0),
        }
        if quick_stop_raw <= 0 or quick_stop_decel > PROFILE_ACCELERATION_LIMIT_UM_S2:
            settings.pop("quick_stop_decel")
            self._post("log", "Device quick-stop deceleration is outside the GUI safety limit; retained the current safe value.")
        if position_window_raw == 0xFFFFFFFF or position_window_raw <= 0:
            settings.pop("position_window")
            self._post("log", "Device position window is disabled or invalid; retained the current safe value.")
        if following_error_raw != 0xFFFFFFFF and following_error_timeout > 0:
            settings["following_error_window"] = self._inc_to_um(float(following_error_raw))
            settings["following_error_timeout"] = following_error_timeout
        else:
            self._post("log", "Device following-error protection is disabled; retained GUI safety defaults.")
        self._post("settings", settings)

    def _apply_profile_worker(self, profile: dict[str, float | int]) -> None:
        linked = self._linked_snapshot()
        if bool(linked["enabled"]):
            nodes = [node for _, node, _ in self._require_linked_axes(linked)]
        else:
            nodes = [self._require_node()]
        for node in nodes:
            self._write_profile(node, profile)

    def _move_absolute_worker(
        self,
        target_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
        *, expected_start_inc: Optional[int] = None,
    ) -> None:
        node = self._require_node()
        target_inc = self._um_to_inc(target_um)

        if self.active_target_inc is not None:
            raise RuntimeError("Motion is already active. Wait for target reached or stop first.")

        self._validate_target(target_um, limits)
        if bool(limits.get("enabled", False)):
            self._apply_limits_worker(limits)
        baseline = self._read_actual_position_inc(node, fresh=True)
        if expected_start_inc is not None:
            self._check_position_reference(expected_start_inc, baseline)
        previous = self.last_position_inc
        if previous is not None:
            self._check_position_reference(previous, baseline, idle_node=node)
        try:
            self._prepare_profile_position(node, profile, baseline)
            if self.test_stop_requested.is_set():
                self._disable_drive_worker()
                return
            start_inc = self._read_actual_position_inc(node, fresh=True)
            self._check_position_reference(baseline, start_inc)
            if expected_start_inc is not None and expected_start_inc != baseline:
                self._check_position_reference(expected_start_inc, start_inc)

            self._start_motion_trace(node, start_inc, target_inc, profile)
            self._invalidate_linked_zero("single-axis move")
            self._command_profile_position(node, target_inc)
            self._post("target_coordinate", self._inc_to_um(target_inc))

            self.runtime_position_window_inc = max(1, int(profile.get("position_window_inc", self.__dict__.get("runtime_position_window_inc", 1))))
            self.active_target_inc = target_inc
            self.active_start_inc = start_inc
            self.active_target_time = time.monotonic()
            distance_um = abs(self._inc_to_um(target_inc - start_inc))
            expected_duration = (
                distance_um / float(profile["velocity"])
                + float(profile["velocity"]) / float(profile["acceleration"])
            )
            self.active_target_deadline = self.active_target_time + max(
                60.0,
                expected_duration * MANUAL_TIMEOUT_FACTOR + MANUAL_TIMEOUT_MARGIN_SECONDS,
            )
            self.active_best_remaining_inc = abs(target_inc - start_inc)
            self.active_progress_samples = [self.active_best_remaining_inc] * 2
            self.active_last_progress_time = self.active_target_time
            self._post("target_start", f"{self._inc_to_um(start_inc):.2f}")
            self._post("target_moved", "0.00")
            self._post("target_remaining", f"{self._inc_to_um(target_inc - start_inc):.2f}")
            self._post("target_progress", "0.0%")
            self._post("single_axis_target", ("目标", target_um))
            self._post("log", f"Commanded absolute target {target_um:.2f} um.")
        except Exception:
            if not self.test_stop_requested.is_set():
                self._stop_and_disable_worker()
            raise

    def _move_relative_worker(
        self,
        delta_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
    ) -> None:
        current_inc = self._read_actual_position_inc(self._require_node(), fresh=True)
        target_um = self._inc_to_um(current_inc) + delta_um
        self._move_absolute_worker(target_um, profile, limits)


    @staticmethod
    def _manual_motion_timeout(distance_um: float, profile: dict[str, float | int]) -> float:
        expected_duration = (
            distance_um / float(profile["velocity"])
            + float(profile["velocity"]) / float(profile["acceleration"])
        )
        return max(60.0, expected_duration * MANUAL_TIMEOUT_FACTOR + MANUAL_TIMEOUT_MARGIN_SECONDS)

    def _run_standard_cycle_worker(self, config: dict[str, object], retreat: bool) -> None:
        try:
            self._run_standard_cycle_worker_impl(config, retreat)
        except BaseException as exc:
            try:
                if not self.stop_pending:
                    self._disable_drive_worker()
            except Exception as stop_exc:
                raise RuntimeError(f"{exc}; disable after workflow failure also failed: {stop_exc}") from exc
            raise

    def _run_standard_cycle_worker_impl(self, config: dict[str, object], retreat: bool) -> None:
        linked = self._linked_snapshot(require_zero=True)
        paired = bool(linked["enabled"])
        panel = self.position_panel
        if not paired and (not panel.user_zero_set or not self.travel_range.confirmed):
            raise ValueError("分段流程需要已确认的收回端点零点，请先置零。")
        offset = 0.0 if paired else panel.user_zero_um
        start, switch, end = (float(config[key]) for key in ("start", "switch", "end"))
        limits = dict(config["limits"])
        base_profile = dict(config["profile"])
        timeout = float(config["timeout"])
        axis = self._active_axis_name()
        node = self._require_node()

        def profile_for(speed: float, acceleration: float, quick_stop: float) -> dict[str, object]:
            if speed > float(base_profile["max_velocity"]):
                raise ValueError("分段速度超过配置的最大速度，已拒绝整个流程。")
            if acceleration > float(base_profile["max_acceleration"]):
                raise ValueError("分段加速度超过配置的最大加速度，已拒绝整个流程。")
            if speed <= 0 or self._um_to_inc(speed) < 1:
                raise ValueError("分段速度量化后为 0 计数/秒，已拒绝整个流程。")
            if acceleration <= 0 or self._um_to_inc(acceleration) < 1:
                raise ValueError("分段加速度量化后为 0 计数/秒²，已拒绝整个流程。")
            if quick_stop <= 0 or self._um_to_inc(quick_stop) < 1:
                raise ValueError("快速停止减速度量化后为 0 计数/秒²，已拒绝整个流程。")
            profile = dict(base_profile)
            profile.update(
                velocity=speed,
                velocity_inc=self._um_to_inc(speed),
                max_velocity=float(base_profile["max_velocity"]),
                max_velocity_inc=int(base_profile["max_velocity_inc"]),
                acceleration=acceleration,
                acceleration_inc=self._um_to_inc(acceleration),
                max_acceleration=float(base_profile["max_acceleration"]),
                max_acceleration_inc=int(base_profile["max_acceleration_inc"]),
                quick_stop_decel=quick_stop,
                quick_stop_decel_inc=self._um_to_inc(quick_stop),
            )
            profile["limits"] = limits
            return profile

        fast_profile = profile_for(
            float(config["fast_speed"]),
            float(config["fast_acceleration"]),
            PROFILE_ACCELERATION_LIMIT_UM_S2,
        )
        slow_profile = profile_for(
            float(config["slow_speed"]),
            float(base_profile["acceleration"]),
            float(base_profile["quick_stop_decel"]),
        )

        window = self._inc_to_um(int(base_profile["position_window_inc"]))
        low_inc, high_inc = self._limit_count_bounds(limits["min"], limits["max"])

        def checked_target(user_target):
            raw = to_encoder(user_target, offset)
            if not limits["min"] <= raw <= limits["max"]:
                raise ValueError(f"分段目标 {user_target:g} µm 超出工作限位。")
            # At a geometric endpoint, choose a reachable count inside the limit.
            count = min(high_inc, max(low_inc, self._um_to_inc(raw)))
            quantized = self._inc_to_um(count)
            self._validate_target(quantized, limits)
            if paired:
                self._validate_linked_targets(self._linked_targets_for_logical_position(quantized, linked), linked)
            return count

        for waypoint in (start, switch, end):
            checked_target(waypoint)

        def read_positions():
            if paired:
                positions = list(self._read_linked_logical_positions(linked, fresh=True).values())
                if max(positions) - min(positions) > linked["tolerance_um"]:
                    raise ValueError("双轴位置差超过联动容差，请核对两轴位置。")
            else:
                positions = [self._inc_to_um(self._read_actual_position_inc(node, fresh=True)) - offset]
            return positions

        def verify_stage(expected):
            actual = read_positions()
            sign = (1 if end > start else -1) * (-1 if retreat else 1)
            actual_target = self._inc_to_um(checked_target(expected[0])) - offset
            reverses_axis = any(sign * (actual_target - pos) < -1e-8 for pos in actual)
            if reverses_axis or next_workflow_stage(actual, start, switch, end, window, retreat) != expected:
                raise RuntimeError("驱动开启后位置跨越分段边界。目标未发送，请复核位置。")

        previous_target = None
        for step_no in (1, 2):
            self._check_motion_cancelled()
            positions = read_positions()
            stage = next_workflow_stage(positions, start, switch, end, window, retreat)
            if stage is None:
                if previous_target is not None:
                    self._disable_drive_worker()
                self._post("workflow_status", "已到工作起点" if retreat else "已到推进终点")
                return
            target, speed = stage
            final = target == (start if retreat else end)
            if target == previous_target or (step_no == 2 and not final):
                raise RuntimeError("分段终点反馈与计划不符，已停止，请核对位置。")
            target_inc = checked_target(target)
            profile = fast_profile if speed == "快速" else slow_profile
            current = " / ".join(f"{position:g}" for position in positions)
            label = f"{speed} {current} → {target:g} µm"
            self._post("workflow_status", label)
            self._post("log", label)
            if paired:
                result = self._run_linked_move(
                    self._linked_targets_for_logical_position(self._inc_to_um(target_inc), linked),
                    profile, timeout, linked, sync_mode="position", disable_after=final, target_label=label,
                    before_setpoint=lambda: verify_stage(stage))
            else:
                self._invalidate_linked_zero("single-axis Standard Cycle")
                result = self._run_test_move(
                    node=node, axis=axis, step_no=step_no, speed_um_s=float(profile["velocity"]),
                    distance_um=target - positions[0], profile_template=profile, step_timeout=timeout,
                    disable_after=final, absolute_target_inc=target_inc, before_setpoint=lambda: verify_stage(stage))
            if result["status"] != "OK":
                self._post("workflow_status", str(result["status"]))
                raise RuntimeError(f"{label}: {result['status']}")
            # The move worker has already verified the quantized final endpoint.
            if final:
                self._post("workflow_status", "已到工作起点" if retreat else "已到推进终点")
                return
            previous_target = target

    def _run_actuator_test_worker(self, test_config: dict[str, object]) -> None:
        linked = self._linked_snapshot()
        if bool(linked["enabled"]):
            self._run_linked_actuator_test_worker(test_config, linked)
            return

        self._invalidate_linked_zero("single-axis test")
        node = self._require_node()
        axis = self._active_axis_name()
        speeds = list(test_config["speeds"])
        distances = list(test_config["distances"])
        settle_time = float(test_config["settle_time"])
        step_timeout = float(test_config["step_timeout"])
        return_after = bool(test_config["return_after"])
        base_profile = dict(test_config["profile"])

        self._post("log", f"Starting actuator test on {axis}.")
        step_no = 1

        for speed in speeds:
            for distance in distances:
                if self.test_stop_requested.is_set():
                    self._post("log", "Actuator test stopped before next step.")
                    return

                result = self._run_test_move(
                    node=node,
                    axis=axis,
                    step_no=step_no,
                    speed_um_s=float(speed),
                    distance_um=float(distance),
                    profile_template=base_profile,
                    step_timeout=step_timeout,
                )
                self._post("test_result", result)
                if result["status"] != "OK":
                    raise RuntimeError(str(result["status"]))
                step_no += 1
                if self.test_stop_requested.wait(settle_time):
                    return

                if return_after:
                    if self.test_stop_requested.is_set():
                        self._post("log", "Actuator test stopped before return step.")
                        return
                    result = self._run_test_move(
                        node=node,
                        axis=axis,
                        step_no=step_no,
                        speed_um_s=float(speed),
                        distance_um=-float(distance),
                        profile_template=base_profile,
                        step_timeout=step_timeout,
                    )
                    self._post("test_result", result)
                    if result["status"] != "OK":
                        raise RuntimeError(str(result["status"]))
                    step_no += 1
                    if self.test_stop_requested.wait(settle_time):
                        return

        self._post("log", "Actuator test complete.")


    def _run_test_move(
        self,
        node: BaseNode402,
        axis: str,
        step_no: int,
        speed_um_s: float,
        distance_um: float,
        profile_template: dict[str, float | int],
        step_timeout: float,
        disable_after: bool = True,
        absolute_target_inc: Optional[int] = None,
        before_setpoint: Optional[Callable[[], None]] = None,
    ) -> dict[str, object]:
        profile = dict(profile_template)
        profile["velocity"] = speed_um_s
        profile["velocity_inc"] = self._um_to_inc(speed_um_s)
        if speed_um_s <= 0 or speed_um_s > float(profile["max_velocity"]):
            raise ValueError("测试速度必须大于 0 且不能超过配置的最大速度。")
        if profile["velocity_inc"] < 1:
            raise ValueError("测试速度量化后为 0 计数/秒，请提高速度或核对编码器型号。")

        start_inc = self._read_actual_position_inc(node, fresh=True)
        start_um = self._inc_to_um(start_inc)
        target_um = start_um + distance_um
        target_inc = self._um_to_inc(target_um)
        if absolute_target_inc is not None:
            target_inc = int(absolute_target_inc)
            target_um = self._inc_to_um(target_inc)
            distance_um = self._inc_to_um(target_inc - start_inc)
        status = "OK"
        start_time = time.monotonic()
        current_inc = start_inc
        end_inc = start_inc
        best_remaining_inc = abs(target_inc - start_inc)
        progress_samples = [best_remaining_inc] * 2
        last_progress_time = start_time
        samples = []

        try:
            self._validate_target(target_um, dict(profile_template["limits"]))
            if bool(dict(profile_template["limits"]).get("enabled")):
                self._apply_limits_worker(dict(profile_template["limits"]))
            self._prepare_profile_position(node, profile, start_inc)

            statusword = self._read_statusword(node, fresh=True)
            if before_setpoint is not None:
                before_setpoint()
            self._command_profile_position(node, target_inc)

            self.runtime_position_window_inc = max(1, int(profile.get("position_window_inc", self.__dict__.get("runtime_position_window_inc", 1))))
            self.active_target_inc = target_inc
            self.active_start_inc = start_inc
            self.active_target_time = time.monotonic()
            self._post("target_start", f"{start_um:.2f}")
            self._post("target_moved", "0.00")
            self._post("target_remaining", f"{distance_um:.2f}")
            self._post("target_progress", "0.0%")
            self._post("single_axis_target", ("目标", target_um))

            deadline = time.monotonic() + step_timeout
            while time.monotonic() < deadline:
                if self.test_stop_requested.is_set():
                    status = "Stopped"
                    break

                current_inc = self._read_actual_position_inc(node, fresh=True)
                self._update_target_progress(current_inc)
                statusword = self._read_statusword(node, fresh=True)
                samples.append({"t_s": time.monotonic() - start_time, "position_inc": current_inc,
                                "statusword": statusword})
                target_reached = bool(statusword & BIT(10))
                remaining_inc = abs(target_inc - current_inc)
                close_enough = remaining_inc <= max(1, self.runtime_position_window_inc)
                if statusword & (BIT(3) | BIT(13)):
                    self._write_controlword(node, 0x0F | BIT(8))
                    status = "Drive fault" if statusword & BIT(3) else "Following error"
                    break
                if target_reached and close_enough:
                    break
                now = time.monotonic()
                best_remaining_inc, last_progress_time, stalled = self._update_progress_watchdog(
                    best_remaining_inc,
                    self._progress_sample(progress_samples, remaining_inc),
                    last_progress_time,
                    now,
                    max(1, self._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
                    MOTION_STALL_TIMEOUT_SECONDS,
                )
                if stalled:
                    self._write_controlword(node, 0x0F | BIT(8))
                    status = (
                        f"Stalled at {self._inc_to_um(current_inc):.2f} um; "
                        f"remaining {self._inc_to_um(target_inc - current_inc):.2f} um"
                    )
                    break
                time.sleep(0.1)
            else:
                status = "Timeout"
                self._write_controlword(node, 0x0F | BIT(8))

            if self.test_stop_requested.is_set():
                end_inc = current_inc
                status = "Stopped"
            else:
                try:
                    end_inc = self._read_actual_position_inc(node, fresh=True)
                except Exception as exc:
                    end_inc = current_inc
                    status = f"{status}; final read error: {exc}"
            if status == "OK":
                statusword = self._read_statusword(node, fresh=True)
                remaining_inc = abs(target_inc - end_inc)
                if statusword & (BIT(3) | BIT(13)):
                    status = "Drive fault during final verification"
                elif not statusword & BIT(10) or remaining_inc > max(1, self.runtime_position_window_inc):
                    status = (
                        f"Endpoint not confirmed: remaining {self._inc_to_um(remaining_inc):.2f} um, "
                        f"status=0x{statusword:04X}"
                    )
        except Exception as exc:
            if self.stop_pending:
                status = "Stopped"
                end_inc = current_inc
            else:
                status = f"Error: {exc}"
                try:
                    self._write_controlword(node, 0x0B)
                except Exception:
                    pass
                try:
                    end_inc = self._read_actual_position_inc(node, fresh=True)
                except Exception:
                    end_inc = current_inc
        finally:
            # Release io_lock promptly when the dedicated STOP worker owns
            # cleanup. Cancellation without that worker still disables here.
            if self.stop_pending:
                status = "Stopped"
            if not self.stop_pending and (disable_after or status != "OK"):
                try:
                    disabled_result = self._disable_drive_worker()
                    if status == "OK":
                        self._verify_disabled_target(node, target_inc, disabled_result)
                except Exception as exc:
                    try:
                        self._write_controlword(node, 0x0B)
                    except Exception:
                        pass
                    status = f"{status}; disable error: {exc}"
            self.active_target_inc = None
            self.active_start_inc = None

        duration = time.monotonic() - start_time
        actual_um = self._inc_to_um(end_inc - start_inc)
        error_um = actual_um - distance_um

        return {
            "step": step_no,
            "axis": axis,
            "speed": speed_um_s,
            "command": distance_um,
            "actual": actual_um,
            "error": error_um,
            "duration": duration,
            "status": status,
            "start_inc": start_inc,
            "target_inc": target_inc,
            "end_inc": end_inc,
            "samples": samples,
        }


    def _home_worker(self, direction_positive: bool, offset_um: float, timeout_seconds: float) -> None:
        self._post("clear_user_zero", None)
        self.travel_range.invalidate()
        self._invalidate_linked_zero("homing")
        node = self._require_node()

        self.active_target_inc = None
        self.active_start_inc = None
        # The original helper sends unguarded enable words even on timeout.
        # Keep the same reference method, but share the STOP cancellation gate.
        try:
            self._motion_state(node, 0x00, 0x4F, 0x40)
            self._motion_state(node, 0x06, 0x6F, 0x21)
            self._motion_state(node, 0x07, 0x6F, 0x23)
            set_node_operation_mode(
                node, NodeOperationMode.HOMING, cancel_check=self._check_motion_cancelled)
            node.sdo["Home offset"].raw = self._um_to_inc(offset_um)
            node.sdo["Homing method"].raw = HomingMethod.POS_INDEX if direction_positive else HomingMethod.NEG_INDEX
            self._motion_state(node, 0x0F, 0x6F, 0x27)
            wait_for_node_operation_mode(
                node, NodeOperationMode.HOMING, cancel_check=self._check_motion_cancelled)
            self._check_motion_cancelled()
            self._write_controlword(node, 0x1F)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                self._check_motion_cancelled()
                status = self._read_statusword(node, fresh=True)
                if status & (BIT(3) | BIT(13)):
                    raise RuntimeError("回零过程中出现驱动故障")
                if status & BIT(12):
                    break
                self.test_stop_requested.wait(0.05)
            else:
                raise RuntimeError("回零超时")
        finally:
            if not self.stop_pending:
                self._disable_drive_worker()
        self._check_motion_cancelled()
        self._post("target", "Homed")
        self._post("log", f"Homing complete with offset {offset_um:.2f} um.")


    def _enable_drive_worker(self, profile: dict[str, float | int]) -> None:
        linked = self._linked_snapshot()
        if bool(linked["enabled"]):
            nodes = [node for _, node, _ in self._require_linked_axes(linked)]
        else:
            nodes = [self._require_node()]
        try:
            for node in nodes:
                self._prepare_profile_position(node, profile)
            self.active_target_inc = None
            self.active_start_inc = None
        except Exception:
            if not self.test_stop_requested.is_set():
                self._disable_drive_worker()
            raise


    def _apply_limits_worker(self, limits_config: dict[str, float | bool]) -> None:
        linked = self._linked_snapshot(require_zero=bool(limits_config["enabled"]))
        if bool(linked["enabled"]):
            self._apply_linked_limits_worker(limits_config, linked)
            return

        node = self._require_node()

        if not bool(limits_config["enabled"]):
            self._clear_limits_worker()
            return

        min_limit = float(limits_config["min"])
        max_limit = float(limits_config["max"])
        low_inc, high_inc = self._limit_count_bounds(min_limit, max_limit)
        limits = node.sdo["Software position limit"]
        limits[1].raw = low_inc
        limits[2].raw = high_inc
        self._post("log", f"Applied runtime limits {min_limit:.2f} to {max_limit:.2f} um.")


    def _clear_limits_worker(self) -> None:
        linked = self._linked_snapshot()
        if bool(linked["enabled"]):
            nodes = [node for _, node, _ in self._require_linked_axes(linked)]
        else:
            nodes = [self._require_node()]
        for node in nodes:
            limits = node.sdo["Software position limit"]
            limits[1].raw = 0
            limits[2].raw = 0
        self._post("settings", {"limits_enabled": False, "min_limit": 0.0, "max_limit": 0.0})
        self._post("log", "Cleared runtime software limits.")

    def _limit_count_bounds(self, low_um: float, high_um: float) -> tuple[int, int]:
        # Round inward; nearest rounding could expand the requested interval.
        low = ceil(low_um / self._inc_to_um(1))
        high = floor(high_um / self._inc_to_um(1))
        if low >= high:
            raise ValueError("工作区间量化后不足两个不同的编码器位置，请调整上下限。")
        return low, high

    def _prepare_profile_position(
        self,
        node: BaseNode402,
        profile: dict[str, float | int],
        reference_inc: Optional[int] = None,
    ) -> None:
        reference = self._read_actual_position_inc(node, fresh=True) if reference_inc is None else int(reference_inc)
        # This firmware latches profile values when the drive is enabled.
        self._write_profile(node, profile)
        self._switch_on_for_position_mode(node, reference)
        node.sdo["Position window"].raw = int(profile["position_window_inc"])
        node.sdo["Position window time"].raw = int(profile["position_window_time"])
        hold_inc = self._initialize_enable_hold(node, reference)
        status = self._motion_state(node, 0x0F, 0x6F, 0x27)
        enabled_inc = self._startup_position_sample(node, "OPERATION ENABLED", status, reference)
        self._check_position_reference(hold_inc, enabled_inc)
        if hold_inc != reference:
            self._check_position_reference(reference, enabled_inc)
        wait_for_node_operation_mode(
            node, NodeOperationMode.PROFILE_POSITION, cancel_check=self._check_motion_cancelled)
        self._check_motion_cancelled()
        status = self._read_statusword(node, fresh=True)
        enabled_inc = self._startup_position_sample(node, "位置模式反馈已确认", status, reference)
        self._check_position_reference(hold_inc, enabled_inc)
        if hold_inc != reference:
            self._check_position_reference(reference, enabled_inc)

    def _switch_on_for_position_mode(self, node, reference):
        for word, mask, expected, stage in (
            (0x00, 0x4F, 0x40, "SWITCH ON DISABLED"),
            (0x06, 0x6F, 0x21, "READY TO SWITCH ON"),
            (0x07, 0x6F, 0x23, "SWITCHED ON"),
        ):
            status = self._motion_state(node, word, mask, expected)
            self._startup_position_sample(node, stage, status, reference)
        set_node_operation_mode(
            node, NodeOperationMode.PROFILE_POSITION, cancel_check=self._check_motion_cancelled)

    def _initialize_enable_hold(self, node, reference_inc):
        # RPDO3 transmits both controlword and target. Replace its stale target
        # while operation is disabled, before transmitting the enable word.
        self._check_motion_cancelled()
        status = self._read_statusword(node, fresh=True)
        if status & 0x6F != 0x23:
            raise RuntimeError("开启驱动需要 SWITCHED ON。操作已取消。")
        hold_inc = self._startup_position_sample(node, "写入保持目标前", status, reference_inc)
        self._check_position_reference(reference_inc, hold_inc)
        self._write_target_position(node, hold_inc)
        readback_inc = int(node.sdo[0x607A].raw)
        self._check_motion_cancelled()
        if readback_inc != hold_inc:
            raise RuntimeError("保持目标读回不一致。驱动开启已取消。")
        self._post("log", f"开启驱动前保持当前位置：{hold_inc} 计数，目标读回已确认。")
        return hold_inc

    def _startup_position_sample(self, node, stage, status, reference):
        self._check_motion_cancelled()
        position = self._read_actual_position_inc(node, fresh=True)
        self._post("log", f"启动采样：节点 {node.id}，{stage}，状态字 0x{status:04X}，"
                   f"0x6064 = {position} 计数（{self._inc_to_um(position):.2f} µm），"
                   f"距启动基线 {position - reference:+d} 计数")
        return position


    def _check_position_reference(self, previous_inc, current_inc, *, idle_node=None):
        change_inc = abs(current_inc - previous_inc)
        tolerance_inc = max(2, self._um_to_inc(POSITION_REFERENCE_TOLERANCE_UM))
        if change_inc <= tolerance_inc:
            if change_inc > 2:
                self._post(
                    "log",
                    f"运动前位置变化 {self._inc_to_um(change_inc):.2f} µm，"
                    f"在 {POSITION_REFERENCE_TOLERANCE_UM:.0f} µm 容限内；绝对目标保持不变。",
                )
            return
        # An old UI sample can predate a manual adjustment. Only the idle
        # comparison may accept this; enable checks always use live samples.
        if idle_node is not None:
            self._check_motion_cancelled()
            status = self._read_statusword(idle_node, fresh=True)
            self._post("log", f"启动采样：节点 {idle_node.id}，状态切换前，状态字 0x{status:04X}，"
                       f"上次 {previous_inc} 计数 → 实读 {current_inc} 计数")
            if operation_disabled(status):
                self.last_position_inc = current_inc
                return
        self.test_stop_requested.set()
        self._record_motion_event("unexpected_displacement", previous_inc=previous_inc, current_inc=current_inc)
        stopped = self._disable_drive_worker()
        self._post("target", "意外位移，已停止。请核对位置")
        raise StartupDisplacement(
            f"检测到意外位移：{self._inc_to_um(previous_inc):.2f} → {self._inc_to_um(current_inc):.2f} µm。"
            "目标已取消，驱动已关闭。请核对位置后再启动。", previous_inc, stopped,
        )

    def _motion_state(self, node, controlword, mask, expected):
        self._check_motion_cancelled()
        self._write_controlword(node, controlword)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self._check_motion_cancelled()
            status = self._read_statusword(node, fresh=True)
            if status & mask == expected:
                return status
            time.sleep(0.05)
        raise RuntimeError(f"Node {node.id}: state transition was not confirmed")


    def _write_profile(self, node: BaseNode402, profile: dict[str, float | int]) -> None:
        velocity = float(profile["velocity"])
        acceleration = float(profile["acceleration"])
        # The device's inherited ~1e9 count/s^3 jerk produces sub-microsecond
        # ramps at our 50 um/s^2 profile. Real-device comparisons show it can
        # overshoot even with a correctly acknowledged single setpoint.
        # Keep acceleration and one continuous trajectory; resolve the ramp
        # with a finite ~0.1 s transition instead of changing the target.
        jerk_um = acceleration * 10.0
        jerk_inc = self._um_to_inc(jerk_um)
        if not 1 <= jerk_inc <= 0xFFFFFFFF:
            raise ValueError("加速度变化率超出控制器可表示范围")
        node.sdo[0x60A4][1].raw = jerk_inc
        if int(node.sdo[0x60A4][1].raw) != jerk_inc:
            raise RuntimeError("加速度变化率读回不一致，运动未开始")
        profile.update(jerk=jerk_um, jerk_inc=jerk_inc)
        node.sdo["Profile velocity"].raw = int(profile["velocity_inc"])
        node.sdo["Profile acceleration"].raw = int(profile["acceleration_inc"])
        node.sdo["Max profile velocity"].raw = int(profile["max_velocity_inc"])
        node.sdo["Max acceleration"].raw = int(profile["max_acceleration_inc"])
        node.sdo["Quick stop deceleration"].raw = int(profile["quick_stop_decel_inc"])
        node.sdo["Following error window"].raw = int(profile["following_error_window_inc"])
        node.sdo["Following error time out"].raw = int(profile["following_error_timeout"])

        self._post(
            "log",
            (
                "Applied profile: "
                f"velocity={velocity:.2f} um/s, acceleration={acceleration:.2f} um/s^2, "
                f"jerk={jerk_um:.2f} um/s^3 (readback verified)."
            ),
        )

    def _command_profile_position(self, node: BaseNode402, target_inc: int) -> None:
        self._command_profile_positions([(node, target_inc)])

    def _command_profile_positions(self, commands: list[tuple[BaseNode402, int]]) -> None:
        command_profile_positions(commands, self._write_controlword, self._write_target_position, self._wait_setpoint_ack)

    def _wait_setpoint_ack(self, nodes, expected: bool) -> None:
        """Read live setpoint acknowledgement; cancellation never re-enables."""
        pending = list(nodes)
        deadline = time.monotonic() + SETPOINT_ACK_TIMEOUT_SECONDS
        while pending:
            self._check_motion_cancelled()
            for node in pending[:]:
                status = self._read_statusword(node, fresh=True)
                self._check_motion_cancelled()
                if status & (BIT(3) | BIT(13)) or status & 0x6F != 0x27:
                    raise RuntimeError("目标握手时驱动故障或未开启，已取消运动")
                if bool(status & BIT(12)) == expected:
                    pending.remove(node)
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("目标确认握手超时，已取消运动")
            self.test_stop_requested.wait(0.01)

    # ------------------------------------------------------------------
    # Status and validation
    # ------------------------------------------------------------------
    def _poll_status(self) -> None:
        start_poll_if_due(self)
        self.schedule(POLL_INTERVAL_MS, self._poll_status)

    def _status_worker(self) -> None:
        poll_feedback(self, rest_seconds=STATUS_POLL_REST_SECONDS)


    def _start_motion_trace(self, node, start_inc, target_inc, profile) -> None:
        self.controller_trace_next_time = 0.0
        self.controller_trace_unsupported = set()
        try:
            self.motion_trace_path = new_trace_path()
            self._record_motion_event("command", node=node.id, start_inc=start_inc, target_inc=target_inc,
                                      um_per_count=self._inc_to_um(1), profile=profile)
            if self.motion_trace_path:
                self._post("log", f"运动轨迹：{self.motion_trace_path}")
        except OSError as exc:
            self.motion_trace_path = None
            self._post("log", f"无法保存轨迹：{exc}")


    def _record_motion_event(self, event: str, **values) -> None:
        if self.motion_trace_path is None:
            return
        try:
            append_event(self.motion_trace_path, event, values)
        except (OSError, TypeError) as exc:
            self.motion_trace_path = None
            self._post("log", f"轨迹写入失败：{exc}")


    def _active_axis_name(self) -> str:
        axis = self.__dict__.get("active_axis")
        return str(axis) if axis is not None else self.active_axis_var.get()

    def _linked_mode_enabled(self) -> bool:
        enabled = self.__dict__.get("linked_mode_enabled")
        return bool(enabled) if enabled is not None else bool(self.linked_mode_var.get())

    def _default_limit_is_enabled(self) -> bool:
        enabled = self.__dict__.get("default_limit_enabled")
        return bool(enabled) if enabled is not None else bool(self.default_limit_var.get())

    def _sync_pair_tolerance(self) -> None:
        try:
            self.pair_tolerance_um = self._parse_positive_float(self.pair_tolerance_var, "maximum A/B mismatch")
        except ValueError:
            # Empty/intermediate text is normal while typing. Invalidate the
            # cached value so a subsequent start cannot silently use an old one.
            self.pair_tolerance_um = float("nan")

    def _validate_default_limit(self, target_um: float) -> None:
        if not self._default_limit_is_enabled():
            return
        linked = self._linked_mode_enabled()
        panel = self.__dict__.get("position_panel")
        if linked:
            if not self.linked_zero_offsets_inc:
                raise ValueError("默认限位下使用双轴联动前，请先设置联动零点。")
            low, high = DEFAULT_WORK_MIN_UM, DEFAULT_WORK_MAX_UM
            user_target = target_um
        else:
            if panel is None or not panel.user_zero_set or not self.travel_range.confirmed:
                raise ValueError("默认限位已开启。请先在收回端点置零。")
            low = to_encoder(DEFAULT_WORK_MIN_UM, panel.user_zero_um)
            high = to_encoder(DEFAULT_WORK_MAX_UM, panel.user_zero_um)
            user_target = to_user(target_um, panel.user_zero_um)

        um_per_count = self._inc_to_um(1)
        target_inc = self._um_to_inc(target_um)
        low_inc = ceil(low / um_per_count)
        high_inc = floor(high / um_per_count)
        if target_um < low or target_um > high or not low_inc <= target_inc <= high_inc:
            raise ValueError(
                f"目标 {user_target:.2f} µm 超出默认限位 "
                f"{DEFAULT_WORK_MIN_UM:.0f}–{DEFAULT_WORK_MAX_UM:.0f} µm。"
                "可在高级设置的行程/零点页取消默认限位。"
            )

    def _validate_target(self, target_um: float, limits: dict[str, float | bool]) -> None:
        self._validate_default_limit(target_um)
        travel = self.__dict__.get("travel_range")
        if not self._linked_mode_enabled():
            if travel is None:
                raise ValueError("缺少机械行程映射，请先确认行程或完成置零。")
            travel.validate(target_um, self._um_to_inc(target_um), self._inc_to_um(1), self._active_axis_name())
        if not bool(limits["enabled"]):
            return

        min_limit = float(limits["min"])
        max_limit = float(limits["max"])
        target_inc = self._um_to_inc(target_um)
        um_per_count = self._inc_to_um(1)
        if (not min_limit <= target_um <= max_limit
                or not ceil(min_limit / um_per_count) <= target_inc <= floor(max_limit / um_per_count)):
            raise ValueError(
                f"Target {target_um:.2f} um is outside the enabled limits "
                f"({min_limit:.2f} to {max_limit:.2f} um)."
            )


    def _connection_snapshot(self) -> dict[str, object]:
        snapshot = {
            "interface": self.interface_var.get().strip(),
            "channel": self.channel_var.get().strip(),
            "bitrate": self._parse_int(self.bitrate_var, "bitrate"),
            "node_id": self._parse_int(self.node_id_var, "node ID"),
            "node_b_enabled": bool(self.node_b_enabled_var.get()),
            "node_b_id": self._parse_int(self.node_b_id_var, "node B ID"),
            "eds_path": Path(self.eds_path_var.get().strip()).expanduser().resolve(),
            "reset_on_connect": bool(self.reset_on_connect_var.get()),
            "boot_timeout": self._parse_positive_float(self.boot_timeout_var, "boot timeout"),
            "encoder": self.encoder_var.get(),
        }

        if snapshot["node_b_enabled"] and snapshot["node_id"] == snapshot["node_b_id"]:
            raise ValueError(
                "Two actuators on one CAN bus must have different Node IDs. "
                "Xeryon devices default to Node ID 32; configure one actuator to another ID first."
            )

        return snapshot

    def _probe_snapshot(self) -> dict[str, object]:
        limit = self._parse_positive_int(self.probe_limit_var, "probe max node ID")
        if limit > 127:
            raise ValueError("Probe max node ID must be 127 or lower.")

        return {
            "interface": self.interface_var.get().strip(),
            "channel": self.channel_var.get().strip(),
            "bitrate": self._parse_int(self.bitrate_var, "bitrate"),
            "limit": limit,
        }

    def _profile_snapshot(self, velocity_override: Optional[float] = None) -> dict[str, float | int]:
        velocity = (self._parse_positive_float(self.velocity_var, "velocity")
                    if velocity_override is None else velocity_override)
        acceleration = self._parse_positive_float(self.acceleration_var, "acceleration")
        max_velocity = self._parse_positive_float(self.max_velocity_var, "maximum velocity")
        max_acceleration = self._parse_positive_float(self.max_acceleration_var, "maximum acceleration")
        quick_stop_decel = self._parse_positive_float(self.quick_stop_decel_var, "quick stop deceleration")
        position_window = self._parse_positive_float(self.position_window_var, "position window")
        position_window_time = self._parse_nonnegative_int(self.position_window_time_var, "position window time")
        following_error_window = self._parse_positive_float(
            self.following_error_window_var, "following error window"
        )
        following_error_timeout = self._parse_positive_int(
            self.following_error_timeout_var, "following error timeout"
        )

        if velocity > max_velocity:
            raise ValueError("所选速度超过速度上限，请在运动参数页核对 Maximum velocity。")
        if acceleration > max_acceleration:
            raise ValueError("Acceleration must not be greater than maximum acceleration.")
        if max_velocity > PROFILE_VELOCITY_LIMIT_UM_S:
            raise ValueError(f"Maximum velocity must not exceed {PROFILE_VELOCITY_LIMIT_UM_S:.0f} um/s.")
        if max(max_acceleration, quick_stop_decel) > PROFILE_ACCELERATION_LIMIT_UM_S2:
            raise ValueError(
                f"Acceleration and deceleration must not exceed {PROFILE_ACCELERATION_LIMIT_UM_S2:.0f} um/s^2."
            )
        if following_error_timeout > 65535:
            raise ValueError("Following error timeout must not exceed 65535 ms.")

        position_window_inc = self._um_to_inc(position_window)
        count_parameters = {
            "速度": self._um_to_inc(velocity),
            "加速度": self._um_to_inc(acceleration),
            "最大速度": self._um_to_inc(max_velocity),
            "最大加速度": self._um_to_inc(max_acceleration),
            "快速停止减速度": self._um_to_inc(quick_stop_decel),
            "到位窗口": position_window_inc,
            "跟随误差窗口": self._um_to_inc(following_error_window),
        }
        zero_count_names = [name for name, count in count_parameters.items() if count < 1]
        if zero_count_names:
            raise ValueError(f"以下参数量化后为 0 计数，无法安全执行：{', '.join(zero_count_names)}。请提高数值或核对编码器型号。")

        return {
            "velocity": velocity,
            "acceleration": acceleration,
            "max_velocity": max_velocity,
            "max_acceleration": max_acceleration,
            "quick_stop_decel": quick_stop_decel,
            "position_window": position_window,
            "position_window_time": position_window_time,
            "position_window_inc": position_window_inc,
            "following_error_window": following_error_window,
            "following_error_timeout": following_error_timeout,
            "following_error_window_inc": self._um_to_inc(following_error_window),
            "velocity_inc": self._um_to_inc(velocity),
            "acceleration_inc": self._um_to_inc(acceleration),
            "max_velocity_inc": self._um_to_inc(max_velocity),
            "max_acceleration_inc": self._um_to_inc(max_acceleration),
            "quick_stop_decel_inc": self._um_to_inc(quick_stop_decel),
        }

    def _test_snapshot(self) -> dict[str, object]:
        speeds = self._parse_number_list(self.test_speeds_var.get(), "test speeds")
        distances = self._parse_number_list(self.test_distances_var.get(), "test distances")
        settle_time = self._parse_nonnegative_float(self.test_settle_var, "settle time")
        step_timeout = self._parse_positive_float(self.test_timeout_var, "step timeout")
        profile = self._profile_snapshot()
        limits = self._limits_snapshot()

        if any(speed <= 0 for speed in speeds):
            raise ValueError("Test speeds must be greater than 0.")
        if any(distance == 0 for distance in distances):
            raise ValueError("Test distances must not be 0.")

        return {
            "speeds": speeds,
            "distances": distances,
            "settle_time": settle_time,
            "step_timeout": step_timeout,
            "return_after": bool(self.test_return_var.get()),
            "profile": {**profile, "limits": limits},
        }

    def _workflow_snapshot(self) -> dict[str, object]:
        start = self._parse_float(self.workflow_start_var, "工作起点")
        switch = self._parse_float(self.workflow_switch_var, "快慢切换点")
        end = self._parse_float(self.workflow_end_var, "推进终点")
        fast_speed = self._parse_positive_float(self.workflow_fast_speed_var, "fast speed")
        slow_speed = self._parse_positive_float(self.workflow_slow_speed_var, "slow speed")
        fast_acceleration = self._parse_positive_float(
            self.workflow_fast_acceleration_var, "fast acceleration"
        )
        travel_limit = self._parse_positive_float(self.workflow_travel_limit_var, "travel limit")
        timeout = self._parse_positive_float(self.workflow_timeout_var, "workflow timeout")

        if slow_speed > fast_speed:
            raise ValueError("慢速段速度不能高于快速段速度。")
        if abs(end - start) > travel_limit:
            raise ValueError("Fast distance plus slow distance must fit inside the travel limit.")
        if max(fast_speed, slow_speed) > PROFILE_VELOCITY_LIMIT_UM_S:
            raise ValueError(f"Workflow speed must not exceed {PROFILE_VELOCITY_LIMIT_UM_S:.0f} um/s.")
        if fast_acceleration > PROFILE_ACCELERATION_LIMIT_UM_S2:
            raise ValueError(
                f"Fast acceleration must not exceed {PROFILE_ACCELERATION_LIMIT_UM_S2:.0f} um/s^2."
            )

        offset = 0.0 if self._linked_mode_enabled() else self.position_panel.user_zero_um
        limits = {"enabled": True, "min": offset + min(start, end), "max": offset + max(start, end)}
        custom_limits = self._limits_snapshot()
        if custom_limits["enabled"]:
            limits["min"] = max(limits["min"], custom_limits["min"])
            limits["max"] = min(limits["max"], custom_limits["max"])
            if limits["min"] >= limits["max"]:
                raise ValueError("分段流程与自定义工作区间没有交集，已拒绝整个流程。")

        return {
            "start": start, "switch": switch, "end": end,
            "fast_speed": fast_speed,
            "slow_speed": slow_speed,
            "fast_acceleration": fast_acceleration,
            "timeout": timeout,
            "limits": limits,
            "profile": self._profile_snapshot(),
        }

    def _parse_number_list(self, text: str, label: str) -> list[float]:
        values: list[float] = []
        for raw in text.replace(";", ",").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                values.append(float(raw))
            except ValueError as exc:
                raise ValueError(f"Invalid {label}: {raw!r}") from exc

        if not values:
            raise ValueError(f"{label.capitalize()} cannot be empty.")
        return values

    def _limits_snapshot(self) -> dict[str, float | bool]:
        enabled = bool(self.limit_enabled_var.get())
        min_limit = self._parse_float(self.min_limit_var, "minimum limit")
        max_limit = self._parse_float(self.max_limit_var, "maximum limit")

        if enabled and min_limit >= max_limit:
            raise ValueError("Minimum limit must be lower than maximum limit.")

        linked = self._linked_mode_enabled()
        if enabled and self.travel_range.confirmed and not linked:
            low, high = self.travel_range.command_bounds
            if min_limit < low or max_limit > high:
                raise ValueError(f"工作区间必须位于已确认行程 {low:.2f} 至 {high:.2f} µm 内。")

        panel = self.__dict__.get("position_panel")
        if enabled and self._default_limit_is_enabled():
            if linked:
                low, high = DEFAULT_WORK_MIN_UM, DEFAULT_WORK_MAX_UM
            elif panel is not None and panel.default_limit_active():
                low = to_encoder(DEFAULT_WORK_MIN_UM, panel.user_zero_um)
                high = to_encoder(DEFAULT_WORK_MAX_UM, panel.user_zero_um)
            else:
                raise ValueError("默认限位下请先完成收回端点置零。")
            if min_limit < low or max_limit > high:
                raise ValueError("自定义工作区间不能超出默认限位 0–13000 µm。")

        return {"enabled": enabled, "min": min_limit, "max": max_limit}

    def _home_snapshot(self) -> tuple[bool, float, float]:
        direction_positive = self.home_direction_var.get() == "Positive index"
        offset_um = self._parse_float(self.home_offset_var, "home offset")
        timeout_seconds = self._parse_positive_float(self.home_timeout_var, "homing timeout")
        return direction_positive, offset_um, timeout_seconds

    def _require_node(self) -> BaseNode402:
        active_axis = self._active_axis_name()
        if active_axis in self.nodes:
            self.node = self.nodes[active_axis]
        if self.node is None:
            raise RuntimeError("Device is not connected.")
        return self.node

    def _um_to_inc(self, value_um: float) -> int:
        return to_counts(value_um, self.inc_per_mm)

    def _inc_to_um(self, value_inc: float) -> float:
        return from_counts(value_inc, self.inc_per_mm)

    def _parse_float(self, variable: Field, label: str) -> float:
        try:
            value = float(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {variable.get()!r}") from exc
        if not isfinite(value):
            raise ValueError(f"{label.capitalize()} must be a finite number.")
        return value

    def _parse_positive_float(self, variable: Field, label: str) -> float:
        value = self._parse_float(variable, label)
        if value <= 0:
            raise ValueError(f"{label.capitalize()} must be greater than 0.")
        return value

    def _parse_nonnegative_float(self, variable: Field, label: str) -> float:
        value = self._parse_float(variable, label)
        if value < 0:
            raise ValueError(f"{label.capitalize()} must be 0 or greater.")
        return value

    def _parse_int(self, variable: Field, label: str) -> int:
        try:
            return int(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {variable.get()!r}") from exc

    def _parse_positive_int(self, variable: Field, label: str) -> int:
        value = self._parse_int(variable, label)
        if value <= 0:
            raise ValueError(f"{label.capitalize()} must be greater than 0.")
        return value

    def _parse_nonnegative_int(self, variable: Field, label: str) -> int:
        value = self._parse_int(variable, label)
        if value < 0:
            raise ValueError(f"{label.capitalize()} must be 0 or greater.")
        return value

    # ------------------------------------------------------------------
    # Main-thread UI updates
    # ------------------------------------------------------------------
    def _post(self, kind: str, payload: object) -> None:
        self.ui_queue.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        # Limit each callback so queued updates cannot starve STOP/Esc or paint.
        deadline = time.monotonic() + UI_EVENT_BUDGET_SECONDS
        pending_logs = []
        try:
            for _ in range(UI_EVENTS_PER_TICK):
                try:
                    kind, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        pending_logs.append(str(payload))
                    else:
                        if pending_logs:
                            self._log_many(pending_logs)
                            pending_logs.clear()
                        self._handle_ui_event(kind, payload)
                except Exception as exc:
                    self._log(f"界面消息 {kind} 处理失败：{exc}")
                    self._show_error(f"界面更新异常：{exc}")
                if time.monotonic() >= deadline:
                    break
            if pending_logs:
                self._log_many(pending_logs)
            self._refresh_ui_state()
        finally:
            # A malformed update must never permanently kill the UI pump.
            self.schedule(10 if not self.ui_queue.empty() else 100, self._drain_ui_queue)

    def _handle_ui_event(self, kind: str, payload: object) -> None:
        if kind == "log":
            self._log(str(payload))
        elif kind == "error":
            self._show_error(str(payload))
        elif kind == "busy":
            self.command_busy = False
            self.busy_var.set(str(payload))
        elif kind == "review_start":
            self.command_busy = False
            self.busy_var.set("Idle")
            self._show_start_confirmation(payload)
        elif kind == "connected":
            self._set_connected_state(bool(payload))
        elif kind == "connection_health":
            healthy = bool(payload)
            self.connected_var.set("Connected" if healthy else "通信故障")
        elif kind == "node_boot":
            if any(node is payload for node in self.nodes.values()):
                self.position_panel.invalidate()
                self._invalidate_linked_zero("controller reboot")
                self.stop_actuator_test()
                self._show_error(self.reconnect_reason)
        elif kind == "command_feedback_fault":
            if any(node is payload for node in self.nodes.values()):
                self.position_panel.received_at = None
                self.stop_actuator_test()
        elif kind == "feedback_stale":
            self._dismiss_start_confirmation()
            self.position_panel.received_at = None
            self._cancel_shortcut_queue("通信中断，流程已取消")
        elif kind == "status":
            self._set_status(payload)
        elif kind == "settings":
            self._set_settings(payload)
        elif kind == "target":
            self.target_status_var.set(str(payload))
        elif kind == "single_axis_target":
            label, raw = payload
            panel = self.position_panel
            self.target_status_var.set(f"{label}，用户位置 {to_user(raw, panel.user_zero_um):+.2f} µm"
                                       if panel.user_zero_set else f"{label}，用户零点未设置")
        elif kind == "target_coordinate":
            self.target_position_var.set(f"{float(payload):.2f}")
        elif kind in ("user_zero", "refreshed_position"):
            if (payload["axis"] == self.active_axis_var.get() and self.node is payload["node"]
                    and payload["generation"] == self.cancel_generation and not self.test_stop_requested.is_set()
                    and not (self.stop_pending or self.stop_unconfirmed or self.close_in_progress
                             or self.disconnect_in_progress or self.reference_review is not None or self.reconnect_reason)):
                if kind == "refreshed_position":
                    self.last_position_inc = payload["position_inc"]
                    self.last_statusword = payload["statusword"]
                    self.reference_positions[payload["axis"]] = self.last_position_inc
                    self._set_status(payload, initialize_target=False)
                    return
                self.position_panel.apply_user_zero(payload["raw_um"], payload["sample_time"])
                self._log(f"编码器已刷新，驱动已关闭并停稳。收回端零点 = {payload['raw_um']:.4f} µm。")
        elif kind == "clear_user_zero":
            self.position_panel.forget_axis_zero()
        elif kind == "target_start":
            self.target_start_var.set(str(payload))
        elif kind == "target_moved":
            self.target_moved_var.set(str(payload))
        elif kind == "target_remaining":
            self.target_remaining_var.set(str(payload))
        elif kind == "target_progress":
            self.target_progress_var.set(str(payload))
            self._set_target_progress_bar(str(payload))
        elif kind == "shortcut_done":
            self._handle_shortcut_done(int(payload))
        elif kind == "shortcut_failed":
            self._handle_shortcut_failed(payload)
        elif kind == "workflow_status":
            self.workflow_status_var.set(str(payload))
        elif kind == "pair_zero_status":
            self.pair_zero_status_var.set(str(payload))
        elif kind == "linked_status":
            if isinstance(payload, dict):
                self.position_a_var.set(f"{float(payload['position_a']):.2f}")
                self.position_b_var.set(f"{float(payload['position_b']):.2f}")
                pair_error = payload.get("pair_error")
                self.pair_error_var.set("-" if pair_error is None else f"{float(pair_error):.2f}")
            else:
                self.position_a_var.set("-")
                self.position_b_var.set("-")
                self.pair_error_var.set("-")
        elif kind == "node_id":
            self.node_id_var.set(str(payload))
        elif kind == "bitrate":
            self.bitrate_var.set(str(payload))
        elif kind == "axes":
            axes = list(payload) if isinstance(payload, list) else ["Actuator A"]
            self.available_axes = axes
            if self.active_axis_var.get() not in axes:
                self.active_axis_var.set(axes[0])
        elif kind == "active_axis":
            self.active_axis_var.set(str(payload))
        elif kind == "clear_test_results":
            self.test_results.clear()
            if self.view:
                self.view.clear_results()
        elif kind == "test_result":
            self._append_test_result(payload)
        elif kind == "repeat_report":
            self.last_report_dir = str(payload["output_dir"])
            groups = payload["endpoints"]
            ranges = "; ".join(f"{k}: 峰峰值 {v['peak_to_peak_inc']} 计数 (n={v['n']})" for k, v in groups.items())
            self.repeat_status_var.set(f"{payload['status']}\n{ranges}（内部编码器重复性）")


    def _refresh_ui_state(self) -> None:
        self.position_panel.redraw()
        self._update_workflow_preview()
        if not self.position_panel.is_fresh():
            self.position_var.set("-")
        if self.view:
            self.view.refresh()

    def _append_test_result(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.test_results.append(dict(payload))

        if self.view:
            self.view.append_result(payload)

    def _set_target_progress_bar(self, value: str) -> None:
        try:
            progress = float(value.strip().rstrip("%"))
        except ValueError:
            progress = 0.0
        self.target_progress_bar_var.set(max(0.0, min(100.0, progress)))

    def _set_connected_state(self, connected: bool) -> None:
        self.connected_var.set("Connected" if connected else "Disconnected")
        if not connected:
            self.position_panel.invalidate()
            self.__dict__.get("travel_ranges", {}).clear()
            self.position_var.set("-")
            self.target_status_var.set("-")
            self.target_start_var.set("-")
            self.target_moved_var.set("-")
            self.target_remaining_var.set("-")
            self.target_progress_var.set("-")
            self.target_progress_bar_var.set(0.0)
            self.statusword_var.set("-")
            self.state_var.set("-")
            self.mode_var.set("-")
            self.workflow_status_var.set("Ready")
            self.pair_zero_status_var.set("Not set")
            self.position_a_var.set("-")
            self.position_b_var.set("-")
            self.pair_error_var.set("-")

    def _set_status(self, payload: object, *, initialize_target=True) -> None:
        if payload is None or isinstance(payload, dict):
            self.position_panel.accept_status(payload, initialize_target=initialize_target)
        if payload is None:
            self.position_var.set("-")
            self.statusword_var.set("-")
            self.state_var.set("-")
            self.mode_var.set("-")
            return

        status = payload
        if not isinstance(status, dict):
            return

        self.position_var.set(f"{status['position_um']:.2f}")
        self.statusword_var.set(f"0x{status['statusword']:04X}")
        self.state_var.set(str(status["state"]))
        self.mode_var.set(str(status["mode"]))

    def _set_settings(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        mapping = {
            "velocity": self.velocity_var,
            "acceleration": self.acceleration_var,
            "max_velocity": self.max_velocity_var,
            "max_acceleration": self.max_acceleration_var,
            "quick_stop_decel": self.quick_stop_decel_var,
            "position_window": self.position_window_var,
            "position_window_time": self.position_window_time_var,
            "following_error_window": self.following_error_window_var,
            "following_error_timeout": self.following_error_timeout_var,
            "min_limit": self.min_limit_var,
            "max_limit": self.max_limit_var,
        }

        for key, variable in mapping.items():
            if key in payload:
                value = payload[key]
                if isinstance(value, float):
                    variable.set(f"{value:.4f}")
                else:
                    variable.set(str(value))

        if "limits_enabled" in payload:
            self.limit_enabled_var.set(bool(payload["limits_enabled"]))
        if "position_window" in payload:
            self.runtime_position_window_inc = max(1, self._um_to_inc(float(payload["position_window"])))

    def _log(self, message: str) -> None:
        self._log_many([message])

    def _log_many(self, messages: list[str]) -> None:
        if self.view:
            self.view.append_logs(messages)

    def _show_error(self, message):
        if self.close_in_progress or self.view is None:
            self._log(message)
        else:
            self.view.show_error(message)

    def _show_close_notice(self):
        self.close_notice = self.view.show_close_notice()

    def schedule(self, delay_ms, callback):
        return self.view.schedule(delay_ms, callback)

    def cancel_timer(self, timer):
        self.view.cancel_timer(timer)

    def finish_exit(self):
        self.view.finish_exit()

    def _validate_start_confirmation(self, review):
        motion = review.motion
        panel = self.position_panel
        self._check_reference_ready()
        if (self.stop_pending or self.stop_unconfirmed or self.close_in_progress or self.disconnect_in_progress
                or motion.generation != self.cancel_generation or self.nodes.get(motion.axis) is not motion.node
                or self._active_axis_name() != motion.axis or self.shortcut_active is not motion.step
                or self._linked_mode_enabled()):
            raise RuntimeError("原启动请求已失效。")
        if (panel.user_zero_um != motion.zero
                or panel.user_zero_set != motion.zero_set or not self.travel_range.confirmed
                or self.travel_range.command_bounds != motion.bounds):
            raise ValueError("零点或行程设置已改变，请重新核对。")
        target = self._parse_float(panel.target, "目标")
        speed = self._parse_positive_float(panel.speed, "速度")
        if not 2 <= speed <= 500:
            raise ValueError("速度须在 2–500 µm/s 范围内，请取消后重新设置。")
        if self._um_to_inc(target) == self._um_to_inc(motion.target):
            target = motion.target
        limits = self._limits_snapshot()
        profile = self._profile_snapshot(speed)
        self._validate_target(target, limits)
        if target == motion.target and limits == motion.limits and profile == motion.profile:
            return review
        if motion.step is not None:
            raise ValueError("快捷步骤的设置已改变，请取消后重新选择流程。")
        return replace(review, motion=replace(motion, target=target, limits=limits, profile=profile))

    def _show_start_confirmation(self, review):
        motion = review.motion
        if (motion.generation != self.cancel_generation or self.close_in_progress or self.disconnect_in_progress
                or self.nodes.get(motion.axis) is not motion.node):
            return
        problem = ""
        original = review
        try:
            review = self._validate_start_confirmation(review)
        except (ValueError, RuntimeError) as exc:
            problem = str(exc)
        motion = review.motion
        self.start_confirmation = review
        old = self._inc_to_um(review.previous_inc)
        current = self._inc_to_um(review.stopped_inc)
        target = self._inc_to_um(self._um_to_inc(motion.target))
        relative = (f"相对用户零点：原起点 {to_user(old, motion.zero):+.2f} → 新起点 {to_user(current, motion.zero):+.2f} µm\n"
                    f"目标 {to_user(target, motion.zero):+.2f} µm" if motion.zero_set else "用户零点未设置")
        message = (f"{motion.axis}  {motion.label}\n驱动已关闭，位置稳定\n{relative}\n"
                   f"预计位移 {target-current:+.2f} µm，速度 {motion.profile['velocity']:g} µm/s。\n"
                   "从新起点继续？")
        if motion.zero_set and review.previous_inc == self._um_to_inc(motion.zero):
            message += "\n仍在收回端点？请取消，再点「收回端点置零」。"
        if original is not review:
            message = "设置已更新，请核对。\n" + message
        absolute = f"编码器绝对位置 {current:.2f} µm"
        if not motion.zero_set:
            absolute += f"\n编码器目标 {target:.2f} µm"
        self._log(message.replace("\n", " "))
        self.target_status_var.set("位置已变化，请确认")
        self.confirmation_problem = problem
        self.view.show_confirmation(message, absolute, problem, review)

    def _block_start_confirmation(self, problem):
        self.confirmation_problem = problem
        self.view.block_confirmation(problem)
        self._log(problem)

    def _dismiss_start_confirmation(self):
        review = self.__dict__.get("start_confirmation")
        if review is None:
            return
        self._close_start_confirmation()
        if review.motion.step is not None:
            self._cancel_shortcut_queue("已取消从新起点继续")
        self.target_status_var.set("已取消运动")

    def _close_start_confirmation(self):
        self.start_confirmation = None
        self.view.close_confirmation()

    def _continue_start(self, review):
        if self.start_confirmation is not review or self.confirmation_problem:
            return
        try:
            updated = self._validate_start_confirmation(review)
        except (ValueError, RuntimeError) as exc:
            self._block_start_confirmation(str(exc))
            return
        if updated is not review:
            self._close_start_confirmation()
            self._show_start_confirmation(review)
            return
        self._close_start_confirmation()
        try:
            self._reset_motion_cancel()
            if self._dispatch_panel_move(review.motion, review.stopped_inc):
                return
        except (ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
        if review.motion.step is not None:
            self._cancel_shortcut_queue("重新启动失败，已取消快捷流程")
        self.target_status_var.set("运动未启动")

    def _on_close(self) -> None:
        if self.close_in_progress:
            if self.close_notice is not None:
                self._show_close_notice()
            return
        self.close_in_progress = True
        self.close_cleanup_started = self.command_busy and self.busy_var.get() == "Disconnect"
        self.disconnect_in_progress = False  # Closing takes over any scheduled disconnect.
        self.close_deadline = time.monotonic() + max(5.0, 2.0 + 1.5 * len(self.nodes))
        if self.__dict__.get("view"):
            self.view.close_dialogs()
        self._cancel_shortcut_queue("快捷流程已关闭")
        self.test_stop_requested.set()
        self._finish_close()

    def _finish_close(self) -> None:
        if not self.close_in_progress:
            return
        if self.nodes and not self.close_stop_requested and not self.close_cleanup_started:
            self.close_stop_requested = True
            self._run_stop_command("Close")

        busy = self.stop_pending or self.command_busy or self.__dict__.get("status_busy", False)
        if not busy and (self.stop_unconfirmed or (self.close_stop_requested and not self.stop_verified)):
            if self.close_notice is None:
                self._show_close_notice()
            return
        if not busy:
            if self.close_cleanup_started or self.network is None:
                self.finish_exit()
                return
            self.close_cleanup_started = True
            try:
                self._run_command("Disconnect", lambda: self._disconnect_worker(verify_stop=False))
            except Exception as exc:
                self._log(f"退出时提交断连失败：{exc}")
                self._show_close_notice()
                return
        if self.close_notice is None and time.monotonic() >= self.close_deadline:
            self._show_close_notice()
        self.schedule(100, self._finish_close)

