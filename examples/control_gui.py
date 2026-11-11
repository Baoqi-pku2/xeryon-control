"""
control_gui.py

Tkinter control panel for a single Xeryon CiA-402 CANopen actuator.

The GUI wraps the existing example helpers without changing persistent device
configuration. Motion parameters and software limits are written at runtime only.

Run from the repository root:
    python examples/control_gui.py

Or from the examples directory:
    python control_gui.py
"""

from __future__ import annotations

import logging
import csv
import json
import os
from datetime import datetime
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from math import ceil, floor, isfinite
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from canopen import BaseNode402, Network
from canopen.sdo.exceptions import SdoAbortedError
from serial.tools import list_ports

EXAMPLES_DIR = Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", EXAMPLES_DIR.parent))
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from common.parameters import CANBitrate, EncoderRes, HomingMethod, NodeOperationMode, NodeState
from repeatability import run_repeatability
from position_panel import PositionPanel, SPEEDS
from ui_theme import (MIN_CLIENT_HEIGHT, MIN_CLIENT_WIDTH, apply_theme,
                      configure_startup_window, scrollable_work_area)
from shortcut_presets import SHORTCUT_PRESETS
from travel_range import DEFAULT_WORK_MAX_UM, DEFAULT_WORK_MIN_UM, TravelRange, next_workflow_stage
from stop_safety import verify_disabled
from common.utils import (
    BIT,
    bound_can_write_timeout,
    error_handler,
    homing,
    lss_read_identity_via_sdo,
    set_controlword,
    set_node_operation_mode,
    set_node_state,
    set_target_position,
    setup_network,
    wait_for_node_operation_mode,
)
from settings import (
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
PDO_MAX_AGE_SECONDS = 1.0
CONTROLLER_TRACE_INTERVAL_SECONDS = 2.0
SETPOINT_ACK_TIMEOUT_SECONDS = 2.0
TPDO3_INHIBIT_TIME_100US = 200
MOTION_PROGRESS_EPSILON_UM = 1.0
MOTION_STALL_TIMEOUT_SECONDS = 10.0
POSITION_REFERENCE_TOLERANCE_UM = 20.0
PAIR_MISMATCH_TIMEOUT_SECONDS = 1.0
MANUAL_TIMEOUT_FACTOR = 2.0
MANUAL_TIMEOUT_MARGIN_SECONDS = 30.0
UM_PER_MM = 1000.0
PROFILE_VELOCITY_LIMIT_UM_S = 400_000.0
PROFILE_ACCELERATION_LIMIT_UM_S2 = 3_000_000.0

COLOR_BG = "#eef2f6"
COLOR_SURFACE = "#ffffff"
COLOR_SURFACE_ALT = "#f8fafc"
COLOR_BORDER = "#cbd5e1"
COLOR_TEXT = "#0f172a"
COLOR_MUTED = "#475569"
COLOR_HEADER = "#0f172a"
COLOR_HEADER_ALT = "#1e293b"
COLOR_ACCENT = "#2563eb"
COLOR_ACCENT_HOVER = "#1d4ed8"
COLOR_SUCCESS = "#15803d"
COLOR_SUCCESS_HOVER = "#166534"
COLOR_DANGER = "#dc2626"
COLOR_DANGER_HOVER = "#b91c1c"
COLOR_WARNING = "#d97706"
COLOR_LOG_BG = "#0f172a"
COLOR_LOG_FG = "#e2e8f0"
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"


@dataclass(frozen=True)
class PanelMotion:
    label: str
    target: float
    profile: dict
    limits: dict
    node: BaseNode402
    axis: str
    zero: float
    zero_set: bool
    displayed_inc: int
    bounds: tuple
    generation: int
    step: Optional[dict]


class StartupDisplacement(RuntimeError):
    def __init__(self, message, previous_inc, stopped):
        super().__init__(message)
        self.previous_inc = previous_inc
        self.stopped = stopped


@dataclass(frozen=True)
class StartConfirmation:
    motion: PanelMotion
    previous_inc: int
    stopped_inc: int


class XeryonControlGui(tk.Tk):
    """Motion-control GUI for one or two Xeryon actuators."""

    def __init__(self) -> None:
        super().__init__()

        self.title("Xeryon 电极推进器控制台 · 分段续行开发版 · 待实机验收")
        self.travel_range = TravelRange()
        small_display = configure_startup_window(self)
        self.inc_per_mm = float(INC_PER_MM)
        self.test_results = []
        self.motion_trace_path = None
        self.controller_trace_next_time = 0.0
        self.controller_trace_unsupported = set()

        self.network = None
        self.node: Optional[BaseNode402] = None
        self.nodes: dict[str, BaseNode402] = {}
        self.io_lock = threading.RLock()
        self.controlword_lock = threading.Lock()
        self.stop_pending = False
        self.stop_verified = False
        self.stop_unconfirmed = False
        self.cancel_generation = 0
        self.command_busy = False
        self.status_busy = False
        self.active_target_inc: Optional[int] = None
        self.active_start_inc: Optional[int] = None
        self.active_target_time = 0.0
        self.active_target_deadline = 0.0
        self.active_best_remaining_inc: Optional[int] = None
        self.active_last_progress_time = 0.0
        self.test_stop_requested = threading.Event()
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
        self.axis_combos: list[ttk.Combobox] = []
        self.channel_combos: list[ttk.Combobox] = []
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
        self.start_confirmation_window = None

        self._build_style()
        self._build_variables()
        self.active_axis_var.trace_add("write", lambda *args: setattr(self, "active_axis", self.active_axis_var.get()))
        self.linked_mode_var.trace_add("write", lambda *args: setattr(self, "linked_mode_enabled", bool(self.linked_mode_var.get())))
        self.invert_b_var.trace_add("write", lambda *args: setattr(self, "invert_b_enabled", bool(self.invert_b_var.get())))
        self.default_limit_var.trace_add("write", lambda *args: setattr(self, "default_limit_enabled", bool(self.default_limit_var.get())))
        self.pair_tolerance_var.trace_add("write", lambda *args: self._sync_pair_tolerance())
        self._build_layout(small_display)
        self._set_connected_state(False)

        self.after(100, self._drain_ui_queue)
        self.after(POLL_INTERVAL_MS, self._poll_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Escape>", lambda event: self.stop_actuator_test())

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------
    def _build_variables(self) -> None:
        default_eds = RESOURCE_ROOT / "eds" / "xeryon_xla_5_eds.eds"
        if not default_eds.exists():
            default_eds = (EXAMPLES_DIR / EDS_PATH).resolve()

        self.serial_ports, default_channel = self._serial_port_choices()

        self.interface_var = tk.StringVar(value=str(CAN_INTERFACE))
        self.channel_var = tk.StringVar(value=default_channel)
        self.bitrate_var = tk.StringVar(value=str(int(CAN_BITRATE)))
        self.node_id_var = tk.StringVar(value=str(NODE_ID))
        self.node_b_enabled_var = tk.BooleanVar(value=False)
        self.node_b_id_var = tk.StringVar(value="33")
        self.active_axis_var = tk.StringVar(value="Actuator A")
        self.linked_mode_var = tk.BooleanVar(value=False)
        self.invert_b_var = tk.BooleanVar(value=False)
        self.pair_tolerance_var = tk.StringVar(value="20.0")
        self.pair_zero_status_var = tk.StringVar(value="Not set")
        self.position_a_var = tk.StringVar(value="-")
        self.position_b_var = tk.StringVar(value="-")
        self.pair_error_var = tk.StringVar(value="-")
        self.eds_path_var = tk.StringVar(value=str(default_eds))
        self.reset_on_connect_var = tk.BooleanVar(value=False)
        self.boot_timeout_var = tk.StringVar(value="8")
        self.probe_limit_var = tk.StringVar(value="64")

        self.velocity_var = tk.StringVar(value="5.0")
        self.acceleration_var = tk.StringVar(value="50.0")
        self.max_velocity_var = tk.StringVar(value="300.0")
        self.max_acceleration_var = tk.StringVar(value="500.0")
        self.quick_stop_decel_var = tk.StringVar(value="500.0")
        self.position_window_var = tk.StringVar(value="5.0")
        self.position_window_time_var = tk.StringVar(value="200")
        self.following_error_window_var = tk.StringVar(value="20.0")
        self.following_error_timeout_var = tk.StringVar(value="1000")

        self.target_position_var = tk.StringVar(value="0.0")
        self.relative_distance_var = tk.StringVar(value="10.0")
        self.jog_step_var = tk.StringVar(value="1.0")
        self.home_direction_var = tk.StringVar(value="Positive index")
        self.home_offset_var = tk.StringVar(value="0.0")
        self.home_timeout_var = tk.StringVar(value="30")

        self.limit_enabled_var = tk.BooleanVar(value=False)
        self.default_limit_var = tk.BooleanVar(value=True)
        self.min_limit_var = tk.StringVar(value="-29000.0")
        self.max_limit_var = tk.StringVar(value="29000.0")

        self.connected_var = tk.StringVar(value="Disconnected")
        self.busy_var = tk.StringVar(value="Idle")
        self.position_var = tk.StringVar(value="-")
        self.user_position_var = tk.StringVar(value="-")
        self.target_status_var = tk.StringVar(value="-")
        self.target_start_var = tk.StringVar(value="-")
        self.target_moved_var = tk.StringVar(value="-")
        self.target_remaining_var = tk.StringVar(value="-")
        self.target_progress_var = tk.StringVar(value="-")
        self.target_progress_bar_var = tk.DoubleVar(value=0.0)
        self.shortcut_status_var = tk.StringVar(value="未选择 · 点击指令可多选，再点击开始运动")
        self.shortcut_dwell_var = tk.StringVar(value="30")
        self.test_speeds_var = tk.StringVar(value="5,10,20")
        self.test_distances_var = tk.StringVar(value="50,100,200")
        self.test_return_var = tk.BooleanVar(value=False)
        self.test_settle_var = tk.StringVar(value="0.2")
        self.test_timeout_var = tk.StringVar(value="60")
        self.workflow_start_var = tk.StringVar(value="50")
        self.workflow_switch_var = tk.StringVar(value="10050")
        self.workflow_end_var = tk.StringVar(value="13000")
        self.workflow_fast_speed_var = tk.StringVar(value="50.0")
        self.workflow_slow_speed_var = tk.StringVar(value="5.0")
        self.workflow_fast_acceleration_var = tk.StringVar(value="500.0")
        self.workflow_travel_limit_var = tk.StringVar(value="15000.0")
        self.workflow_timeout_var = tk.StringVar(value="900")
        self.workflow_preview_var = tk.StringVar(value="等待有效位置反馈")
        self.workflow_status_var = tk.StringVar(value="Ready")
        self.statusword_var = tk.StringVar(value="-")
        self.state_var = tk.StringVar(value="-")
        self.mode_var = tk.StringVar(value="-")
        self.encoder_var = tk.StringVar(value="1MU")
        self.repeat_cycles_var = tk.StringVar(value="3")
        self.repeat_span_var = tk.StringVar(value="10")
        self.repeat_speed_var = tk.StringVar(value="5")
        self.repeat_status_var = tk.StringVar(value="待测试；结果为编码器重复性，不代表外部绝对精度")
        self.last_report_dir = None

    def _build_style(self) -> None:
        apply_theme(self)

    def _build_layout(self, small_display: bool) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        root = ttk.Frame(self, style="App.TFrame", padding=(12, 8, 12, 8))
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self._build_header(root)

        notebook_parent = root
        if small_display:
            header = self.header_title.master
            viewport, notebook_parent = scrollable_work_area(
                root, MIN_CLIENT_WIDTH - 24, MIN_CLIENT_HEIGHT - header.winfo_reqheight() - 24)
            viewport.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        notebook = ttk.Notebook(notebook_parent, style="Main.TNotebook")
        if small_display:
            notebook.pack(fill="both", expand=True)
        else:
            notebook.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        self.notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", lambda event: self.page_var.set(notebook.index("current")))
        position_tab = ttk.Frame(notebook, style="App.TFrame")
        notebook.add(position_tab, text="控制台")
        advanced_outer = ttk.Frame(notebook)
        notebook.add(advanced_outer, text="高级设置")
        advanced_outer.columnconfigure(0, weight=1)
        advanced_outer.rowconfigure(0, weight=1)
        self.advanced_notebook = ttk.Notebook(advanced_outer)
        self.advanced_notebook.grid(row=0, column=0, sticky="nsew")
        quick_tab = self._scroll_tab(self.advanced_notebook, "高级控制")
        device_tab = self._scroll_tab(self.advanced_notebook, "设备连接")
        motion_tab = self._scroll_tab(self.advanced_notebook, "运动参数")
        test_tab = self._scroll_tab(self.advanced_notebook, "重复定位 / 测试")
        limits_tab = self._scroll_tab(self.advanced_notebook, "行程 / 零点")
        log_tab = self._scroll_tab(self.advanced_notebook, "运行日志")

        position_tab.columnconfigure(0, weight=1)
        position_tab.rowconfigure(0, weight=1)
        self.position_panel = PositionPanel(position_tab, self)
        self.position_panel.grid(row=0, column=0, sticky="nsew")
        self._build_quick_tab(quick_tab)
        self._build_device_tab(device_tab)
        self._build_motion_tab(motion_tab)
        self._build_test_tab(test_tab)
        self._build_limits_tab(limits_tab)
        self._build_log_tab(log_tab)

    def _scroll_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        page = ttk.Frame(notebook, style="App.TFrame")
        notebook.add(page, text=title)
        return page

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header.columnconfigure(2, weight=1)
        self.header_title = ttk.Label(header, text="电极推进器", style="CompactTitle.TLabel")
        self.header_title.grid(row=0, column=0, sticky="w", padx=(0, 16))
        navigation = ttk.Frame(header, style="Header.TFrame")
        navigation.grid(row=0, column=1, sticky="w")
        self.page_var = tk.IntVar(value=0)
        self.page_buttons = []
        for index, label in enumerate(("控制台", "高级设置")):
            button = ttk.Radiobutton(navigation, text=label, variable=self.page_var, value=index,
                                     style="Nav.TRadiobutton", command=lambda i=index: self.notebook.select(i))
            button.pack(side="left", padx=(0, 4))
            self.page_buttons.append(button)
        self._build_navigation_status(header)
        self.global_stop_button = ttk.Button(header, text="停止并失能  STOP / Esc", style="Danger.TButton",
                                            command=self.stop_actuator_test)
        self.global_stop_button.grid(row=0, column=3, sticky="e", padx=(12, 0))
        fitted_width = [None]
        def fit_status(event):
            if event.width == fitted_width[0]:
                return
            fitted_width[0] = event.width
            other_status = sum(w.winfo_reqwidth() for w in self.navigation_status.winfo_children()
                               if w is not self.header_busy_label)
            available = (event.width - self.header_title.winfo_reqwidth() - navigation.winfo_reqwidth()
                         - self.global_stop_button.winfo_reqwidth() - other_status - 82)
            self.header_busy_label.configure(wraplength=max(40, min(220, available)))
        header.bind("<Configure>", fit_status)

    def _build_navigation_status(self, header: ttk.Frame) -> None:
        status = ttk.Frame(header, style="Header.TFrame")
        status.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.navigation_status = status
        connection_text = tk.StringVar(value="未连接")
        busy_text = tk.StringVar(value="空闲")
        self.connected_var.trace_add("write", lambda *_: connection_text.set(
            {"Connected": "已连接", "Disconnected": "未连接"}.get(self.connected_var.get(), self.connected_var.get())))
        self.busy_var.trace_add("write", lambda *_: busy_text.set(
            {"Idle": "空闲"}.get(self.busy_var.get(), self.busy_var.get())))
        self.header_connected_label = ttk.Label(status, textvariable=connection_text,
                                                style="HeaderMetricDisconnected.TLabel")
        self.header_connected_label.pack(side="left", padx=(0, 4))
        ttk.Label(status, text="控制器", style="HeaderMetricLabel.TLabel").pack(side="left", padx=(0, 2))
        self.header_busy_label = ttk.Label(status, textvariable=busy_text, style="HeaderMetricValue.TLabel", wraplength=220)
        self.header_busy_label.pack(side="left")
        ttk.Label(status, textvariable=self.active_axis_var, style="HeaderSubtitle.TLabel").pack(side="right", padx=(8, 0))

    def _header_metric(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int,
                       value_style: str = "HeaderMetricValue.TLabel", width: int = 10) -> ttk.Label:
        box = ttk.Frame(parent, style="HeaderMetric.TFrame")
        box.grid(row=0, column=column, sticky="e", padx=(12, 0))
        ttk.Label(box, text=label, style="HeaderMetricLabel.TLabel").grid(row=0, column=0, sticky="w")
        val = ttk.Label(box, textvariable=variable, style=value_style, width=width, anchor="center")
        val.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        return val

    def _build_quick_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=2)
        parent.rowconfigure(0, weight=1)

        workflow = ttk.LabelFrame(parent, text="分段进退（长行程）", padding=(14, 10))
        workflow.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)
        workflow.columnconfigure(1, weight=1)
        self._entry(workflow, "工作起点 (µm)", self.workflow_start_var, 0, 0)
        self._entry(workflow, "快慢切换点 (µm)", self.workflow_switch_var, 1, 0)
        self._entry(workflow, "推进终点 (µm)", self.workflow_end_var, 2, 0)
        self._entry(workflow, "快速段速度 (µm/s)", self.workflow_fast_speed_var, 3, 0)
        self._entry(workflow, "慢速段速度 (µm/s)", self.workflow_slow_speed_var, 4, 0)
        self._readonly(workflow, "阶段", self.workflow_status_var, 5, 0)
        ttk.Label(workflow, textvariable=self.workflow_preview_var, wraplength=325).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        workflow_buttons = ttk.Frame(workflow)
        workflow_buttons.grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        for column in range(3):
            workflow_buttons.columnconfigure(column, weight=1)
        ttk.Button(
            workflow_buttons,
            text="分段推进",
            style="Good.TButton",
            command=lambda: self.run_standard_cycle(False),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(
            workflow_buttons,
            text="分段退出",
            command=lambda: self.run_standard_cycle(True),
        ).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(
            workflow_buttons,
            text="STOP",
            style="Danger.TButton",
            command=self.stop_standard_cycle,
        ).grid(row=0, column=2, sticky="ew", padx=(3, 0))

        motion = ttk.LabelFrame(parent, text="高级运动（单轴用户坐标 / 双轴联动坐标）", padding=(14, 10))
        motion.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        motion.columnconfigure(1, weight=1)
        motion.columnconfigure(3, weight=1)

        self._readonly(motion, "本轴用户位置 (µm)", self.user_position_var, 0, 0)
        self._readonly(motion, "剩余距离 (µm)", self.target_remaining_var, 0, 2)
        self._entry(motion, "目标位置 (µm)", self.position_panel.user_target, 1, 0)
        self._entry(motion, "速度 (µm/s)", self.velocity_var, 1, 2)
        ttk.Button(motion, text="移动到目标位置", style="Good.TButton", command=self.move_to_target).grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(12, 6)
        )
        ttk.Button(motion, text="STOP", style="Danger.TButton", command=self.quick_stop).grid(
            row=2, column=3, sticky="ew", padx=8, pady=(12, 6)
        )
        ttk.Progressbar(
            motion,
            variable=self.target_progress_bar_var,
            maximum=100.0,
            mode="determinate",
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=(8, 4))
        self._readonly(motion, "已移动 (µm)", self.target_moved_var, 4, 0)
        self._readonly(motion, "进度", self.target_progress_var, 4, 2)

        self._entry(motion, "相对距离 (µm)", self.relative_distance_var, 5, 0)
        relative_buttons = ttk.Frame(motion)
        relative_buttons.grid(row=5, column=2, columnspan=2, sticky="ew", padx=8, pady=5)
        relative_buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(relative_buttons, text="正向 +", command=lambda: self.move_relative(+1)).grid(
            row=0, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(relative_buttons, text="负向 −", command=lambda: self.move_relative(-1)).grid(
            row=0, column=1, sticky="ew", padx=(3, 0)
        )
        self._entry(motion, "点动步长 (µm)", self.jog_step_var, 6, 0)
        jog_buttons = ttk.Frame(motion)
        jog_buttons.grid(row=6, column=2, columnspan=2, sticky="ew", padx=8, pady=5)
        jog_buttons.columnconfigure((0, 1), weight=1)
        ttk.Button(jog_buttons, text="点动 +", command=lambda: self.jog(+1)).grid(
            row=0, column=0, sticky="ew", padx=(0, 3)
        )
        ttk.Button(jog_buttons, text="点动 −", command=lambda: self.jog(-1)).grid(
            row=0, column=1, sticky="ew", padx=(3, 0)
        )

        actions = ttk.Frame(motion)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", padx=8, pady=(12, 4))
        actions.columnconfigure((0, 1, 2, 3), weight=1)
        ttk.Button(actions, text="寻找参考点", command=self.home_device).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(actions, text="移到工作起点", command=self.move_to_work_start).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(actions, text="驱动使能", command=self.enable_drive).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(actions, text="驱动失能", style="Danger.TButton", command=self.disable_drive).grid(
            row=0, column=3, sticky="ew", padx=(4, 0)
        )

        linked = ttk.Frame(motion)
        linked.grid(row=8, column=0, columnspan=4, sticky="ew", padx=8, pady=(12, 4))
        linked.columnconfigure(3, weight=1)
        self.linked_mode_check = ttk.Checkbutton(linked, text="双轴联动", variable=self.linked_mode_var)
        self.linked_mode_check.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.invert_b_check = ttk.Checkbutton(linked, text="反转 B 方向", variable=self.invert_b_var)
        self.invert_b_check.grid(row=0, column=1, sticky="w", padx=(0, 12))
        self.set_pair_zero_button = ttk.Button(linked, text="设置联动零点", command=self.set_linked_zero)
        self.set_pair_zero_button.grid(row=0, column=2, sticky="e")
        ttk.Label(linked, textvariable=self.pair_zero_status_var, style="Muted.TLabel").grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )
        self._entry(motion, "最大 A/B 偏差 (µm)", self.pair_tolerance_var, 9, 0)
        self._readonly(motion, "A 位置 (µm)", self.position_a_var, 9, 2)
        self._readonly(motion, "B 位置 (µm)", self.position_b_var, 10, 0)
        self._readonly(motion, "A/B 偏差 (µm)", self.pair_error_var, 10, 2)

    def _build_device_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(3, weight=1)
        parent.rowconfigure(1, weight=1)

        connection = ttk.LabelFrame(parent, text="连接设置", padding=(14, 10))
        connection.grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(12, 8))
        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(3, weight=1)

        self._entry(connection, "Interface", self.interface_var, 0, 0)
        ttk.Label(connection, text="Channel", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=8, pady=5
        )
        channel_row = ttk.Frame(connection)
        channel_row.grid(row=0, column=3, sticky="ew", padx=8, pady=5)
        channel_row.columnconfigure(0, weight=1)
        channel_combo = ttk.Combobox(channel_row, textvariable=self.channel_var, values=self.serial_ports)
        channel_combo.grid(row=0, column=0, sticky="ew")
        self.channel_combos.append(channel_combo)
        ttk.Button(channel_row, text="刷新", command=self.refresh_ports).grid(row=0, column=1, padx=(6, 0))

        bitrates = [str(int(rate)) for rate in CANBitrate]
        ttk.Label(connection, text="Bitrate").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        ttk.Combobox(connection, textvariable=self.bitrate_var, values=bitrates, width=18).grid(
            row=1, column=1, sticky="ew", padx=8, pady=5
        )
        self._entry(connection, "Node A ID", self.node_id_var, 1, 2)

        ttk.Checkbutton(
            connection,
            text="启用 B 轴",
            variable=self.node_b_enabled_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=5)
        self._entry(connection, "Node B ID", self.node_b_id_var, 2, 2)

        ttk.Label(connection, text="EDS file").grid(row=3, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(connection, textvariable=self.eds_path_var).grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=8, pady=5
        )

        self._entry(connection, "Boot timeout (s)", self.boot_timeout_var, 4, 0)
        ttk.Label(connection, text="编码器型号（连接前选择）").grid(row=6, column=0, sticky="w", padx=8)
        self.encoder_combo = ttk.Combobox(connection, textvariable=self.encoder_var,
                                         values=["10MU", "1MU", "250NAN", "100NAN"], state="readonly")
        self.encoder_combo.grid(row=6, column=1, sticky="ew", padx=8)
        ttk.Checkbutton(
            connection,
            text="连接时复位",
            variable=self.reset_on_connect_var,
        ).grid(row=4, column=2, columnspan=2, sticky="w", padx=8, pady=5)

        buttons = ttk.Frame(connection)
        buttons.grid(row=5, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        ttk.Button(buttons, text="连接设备", style="Good.TButton", command=self.connect_device).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="断开连接", command=self.disconnect_device).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="读取参数", command=self.read_device_settings).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="扫描节点", command=self.probe_node_ids).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="扫描波特率", command=self.probe_bitrates).pack(side="left", padx=(0, 8))
        ttk.Label(buttons, text="Probe max").pack(side="left", padx=(12, 4))
        ttk.Entry(buttons, textvariable=self.probe_limit_var, width=6).pack(side="left")

        status = ttk.LabelFrame(parent, text="实时状态", padding=(14, 10))
        status.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=12, pady=8)
        status.columnconfigure(1, weight=1)
        status.columnconfigure(3, weight=1)

        self._readonly(status, "Connection", self.connected_var, 0, 0)
        self._readonly(status, "Command", self.busy_var, 0, 2)
        self._readonly(status, "编码器绝对位置 (µm)", self.position_var, 1, 0)
        self._readonly(status, "Target", self.target_status_var, 1, 2)
        self._readonly(status, "CiA-402 state", self.state_var, 2, 0)
        self._readonly(status, "Mode", self.mode_var, 2, 2)
        self._readonly(status, "Statusword", self.statusword_var, 3, 0)
        self._readonly(status, "A position (um)", self.position_a_var, 3, 2)
        ttk.Label(status, text="Active actuator").grid(row=4, column=0, sticky="w", padx=8, pady=5)
        axis_combo = ttk.Combobox(
            status,
            textvariable=self.active_axis_var,
            values=["Actuator A"],
            state="readonly",
            width=18,
        )
        axis_combo.grid(row=4, column=1, sticky="ew", padx=8, pady=5)
        self.axis_combos.append(axis_combo)
        self._readonly(status, "B position (um)", self.position_b_var, 4, 2)
        self._readonly(status, "A/B mismatch (um)", self.pair_error_var, 5, 2)

        safety = ttk.LabelFrame(parent, text="Safety Note", padding=(14, 10))
        safety.grid(row=2, column=0, columnspan=4, sticky="ew", padx=12, pady=(8, 12))
        ttk.Label(
            safety,
            text=(
                "The software stop controls CANopen state only. Keep hardware STO, power switch, "
                "mechanical end stops, and low-speed testing in place."
            ),
            wraplength=900,
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=8, pady=8)

    def _build_motion_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=3)
        parent.columnconfigure(1, weight=2)

        profile = ttk.LabelFrame(parent, text="运动参数（仅本次运行，不写入 Flash）", padding=(14, 10))
        profile.grid(row=0, column=0, sticky="new", padx=12, pady=12)
        profile.columnconfigure(1, weight=1)
        profile.columnconfigure(3, weight=1)

        self._entry(profile, "速度 (µm/s)", self.velocity_var, 0, 0)
        self._entry(profile, "加速度 (µm/s²)", self.acceleration_var, 0, 2)
        self._entry(profile, "最大速度 (µm/s)", self.max_velocity_var, 1, 0)
        self._entry(profile, "最大加速度 (µm/s²)", self.max_acceleration_var, 1, 2)
        self._entry(profile, "快速停止减速度 (µm/s²)", self.quick_stop_decel_var, 2, 0)
        self._entry(profile, "到位窗口 (µm)", self.position_window_var, 2, 2)
        self._entry(profile, "窗口保持时间 (ms)", self.position_window_time_var, 3, 0)
        self._entry(profile, "跟随误差窗口 (µm)", self.following_error_window_var, 3, 2)
        self._entry(profile, "跟随误差超时 (ms)", self.following_error_timeout_var, 4, 0)
        ttk.Button(profile, text="应用运动参数", command=self.apply_motion_profile).grid(
            row=4, column=2, columnspan=2, sticky="ew", padx=8, pady=5
        )
        ttk.Label(profile, text="平滑加速：自动设置约 0.1 秒加速度过渡，保持设定加速度。",
                  style="Muted.TLabel").grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=5)

        home = ttk.LabelFrame(parent, text="回零参数", padding=(14, 10))
        home.grid(row=0, column=1, sticky="new", padx=12, pady=12)
        home.columnconfigure(1, weight=1)
        ttk.Label(home, text="方向", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=8, pady=5)
        ttk.Combobox(
            home,
            textvariable=self.home_direction_var,
            values=["Positive index", "Negative index"],
            state="readonly",
        ).grid(row=0, column=1, sticky="ew", padx=8, pady=5)
        self._entry(home, "偏移 (µm)", self.home_offset_var, 1, 0)
        self._entry(home, "超时 (s)", self.home_timeout_var, 2, 0)
        ttk.Label(
            home,
            text="运动、点动和回零操作统一放在“高级控制”；主页面只保留最常用的单轴目标运动。",
            style="Muted.TLabel",
            wraplength=360,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(16, 4))

    def _build_limits_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        self.position_panel.build_travel_settings(parent)

        limits = ttk.LabelFrame(parent, text="可选工作区间（编码器坐标，只能缩小已确认行程）", padding=(14, 10))
        limits.grid(row=1, column=0, sticky="ew", padx=12, pady=(12, 8))
        limits.columnconfigure(1, weight=1)
        limits.columnconfigure(3, weight=1)

        ttk.Checkbutton(limits, text="启用更窄的工作区间", variable=self.limit_enabled_var).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=6
        )
        self._entry(limits, "最小坐标 (µm)", self.min_limit_var, 1, 0)
        self._entry(limits, "最大坐标 (µm)", self.max_limit_var, 1, 2)
        ttk.Button(limits, text="写入设备临时限位", command=self.apply_limits).grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=8
        )
        ttk.Button(limits, text="清除设备临时限位", command=self.clear_limits).grid(
            row=2, column=2, columnspan=2, sticky="ew", padx=8, pady=8
        )

        ttk.Label(
            parent,
            text=(
                "设备临时限位与上方的软件行程映射相互独立；清除设备限位不会取消已确认的软件行程边界。"
                "安装件、负载和排线可能进一步限制实际可用行程。"
            ),
            wraplength=900,
            justify="left",
        ).grid(row=2, column=0, sticky="w", padx=18, pady=6)

    def _build_test_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        config = ttk.LabelFrame(parent, text="功能测试与重复定位", padding=(14, 10))
        config.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        config.columnconfigure(1, weight=1)
        config.columnconfigure(3, weight=1)

        self._entry(config, "速度列表 (µm/s)", self.test_speeds_var, 0, 0)
        self._entry(config, "距离列表 (µm)", self.test_distances_var, 0, 2)
        self._entry(config, "间隔 (s)", self.test_settle_var, 1, 0)
        self._entry(config, "单步超时 (s)", self.test_timeout_var, 1, 2)
        ttk.Checkbutton(config, text="每步完成后反向移动", variable=self.test_return_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8, pady=5
        )

        buttons = ttk.Frame(config)
        buttons.grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        ttk.Button(buttons, text="运行功能测试", style="Good.TButton", command=self.run_actuator_test).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="停止并失能", style="Danger.TButton", command=self.stop_actuator_test).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="导出 CSV", command=self.export_test_results).pack(side="left", padx=8)
        self._entry(config, "重复次数（2–20）", self.repeat_cycles_var, 4, 0)
        self._entry(config, "端点间距（计数）", self.repeat_span_var, 4, 2)
        self._entry(config, "速度（计数/秒）", self.repeat_speed_var, 5, 0)
        repeat_buttons = ttk.Frame(config)
        repeat_buttons.grid(row=5, column=2, columnspan=2, sticky="w", padx=8)
        ttk.Button(repeat_buttons, text="开始重复定位", style="Good.TButton",
                   command=self.start_repeatability).pack(side="left", padx=(0, 8))
        ttk.Button(repeat_buttons, text="打开结果文件夹", command=self.open_report_folder).pack(side="left")
        ttk.Label(config, textvariable=self.repeat_status_var, wraplength=920).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=8, pady=6)

        result_frame = ttk.LabelFrame(parent, text="测试结果（µm 为所选编码器的换算值）", padding=(10, 8))
        result_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(8, 12))
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        columns = ("step", "axis", "speed", "command", "actual", "error", "duration", "status")
        self.test_tree = ttk.Treeview(result_frame, columns=columns, show="headings", height=8)
        headings = {
            "step": "Step",
            "axis": "Axis",
            "speed": "Speed um/s",
            "command": "Command um",
            "actual": "Actual um",
            "error": "Error um",
            "duration": "Time s",
            "status": "Status",
        }
        widths = {
            "step": 50,
            "axis": 90,
            "speed": 90,
            "command": 100,
            "actual": 100,
            "error": 90,
            "duration": 80,
            "status": 120,
        }
        for column in columns:
            self.test_tree.heading(column, text=headings[column])
            self.test_tree.column(column, width=widths[column], anchor="center")
        self.test_tree.tag_configure("ok", foreground=COLOR_SUCCESS)
        self.test_tree.tag_configure("warn", foreground=COLOR_WARNING)
        self.test_tree.tag_configure("error", foreground=COLOR_DANGER)
        self.test_tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(result_frame, orient="vertical", command=self.test_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.test_tree.configure(yscrollcommand=scrollbar.set)

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            parent,
            height=24,
            state="disabled",
            background=COLOR_LOG_BG,
            foreground=COLOR_LOG_FG,
            insertbackground=COLOR_LOG_FG,
            selectbackground=COLOR_ACCENT,
            relief="flat",
            borderwidth=0,
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

    def _entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=column, sticky="w", padx=8, pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=column + 1, sticky="ew", padx=8, pady=5)

    def _readonly(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=column, sticky="w", padx=8, pady=5)
        ttk.Label(parent, textvariable=variable, style="ReadonlyField.TLabel").grid(
            row=row, column=column + 1, sticky="ew", padx=8, pady=5
        )

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
        for combo in self.channel_combos:
            combo.configure(values=self.serial_ports)
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
            messagebox.showinfo("导出", "还没有测试结果。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                          initialfile="encoder_results.csv", filetypes=[("CSV", "*.csv")])
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
                "output_dir": str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XeryonControl" / "results"),
            }
            if not messagebox.askokcancel("空载往返测试", "确认推杆空载、处于中部且两端有余量。\n"
                    "将在当前坐标与正向小行程端点之间往返，结束后失能。\n"
                    "测试以编码器计数为准；绝对精度需要外部量具。", parent=self):
                return
            self._reset_motion_cancel()
            self.repeat_status_var.set("正在测试；点击顶部 STOP 可中止")
            def run():
                result = run_repeatability(self, config)
                self._post("repeat_report", result)
            self._safe_start("重复定位", run)
        except Exception as exc:
            self._show_error(str(exc))

    def disconnect_device(self) -> None:
        if self.disconnect_in_progress or self.close_in_progress:
            return
        self.disconnect_in_progress = True
        self._cancel_shortcut_queue("快捷流程已断开")
        self.test_stop_requested.set()
        if self.nodes:
            self._run_stop_command("Disconnect", self._stop_and_disable_worker)
        self.after(100, self._finish_disconnect)

    def _finish_disconnect(self) -> None:
        if not self.disconnect_in_progress:
            return
        if self.stop_pending or self.command_busy or self.__dict__.get("status_busy", False):
            self.after(100, self._finish_disconnect)
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
                target_um += panel.user_zero_um
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
        """Capture UI values on the Tk thread; only Start can submit this move."""
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
            self.target_status_var.set("未启动 · 请查看提示或运行日志")
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
        target = panel.user_zero_um + user_target
        try:
            self.travel_range.validate(target, self._um_to_inc(target), self._inc_to_um(1), axis)
        except ValueError as exc:
            raise ValueError(
                f"快捷 {number} · {name}：用户目标 {user_target:g} µm，"
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
            self.target_status_var.set("未启动 · 请查看提示或运行日志")
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
                self._log(f"快捷 {number} · {step['name']} 已加入队列。")
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

        if self._submit_panel_move(f"快捷 {number} · {name}", float(step["target"]),
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
                if approved_start is not None and not self._operation_disabled(
                        self._read_statusword(motion.node, fresh=True)):
                    raise RuntimeError("等待确认期间驱动状态已改变，继续请求已取消。")
                expected = motion.displayed_inc if approved_start is None else approved_start
                # The driver adds derived jerk values to its working profile.
                # Keep the operator's original settings for the confirmation check.
                self._move_absolute_worker(motion.target, dict(motion.profile), motion.limits,
                                           expected_start_inc=expected)
            except StartupDisplacement as exc:
                stopped = exc.stopped.get(motion.node.id) if isinstance(exc.stopped, dict) else None
                if stopped and self._operation_disabled(stopped["statusword"]):
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
                self.shortcut_status_var.set(f"已选择：{selected} ｜ 点击“开始运动”依次执行")
            else:
                self.shortcut_status_var.set("未选择 · 点击指令可多选，再点击开始运动")
            return
        deadline = self.__dict__.get("shortcut_wait_deadline")
        if deadline is not None:
            current = f"步骤 {active['number']} 已到位 · 停留 {max(0, ceil(deadline - time.monotonic()))} 秒后自动继续"
        else:
            current = f"正在 {active['number']} · {active['name']}"
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
        self._log(f"快捷 {active['number']} · {active['name']} 已到位并失能。")
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
        self.shortcut_status_var.set("快捷流程完成 · 全部到位")
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
            self.shortcut_wait_timer = self.after(100, lambda: self._continue_shortcut_after_dwell(generation))
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
                self.shortcut_status_var.set("停留结束 · 等待新位置反馈，尚未启动下一步")
                self.shortcut_wait_timer = self.after(100, lambda: self._continue_shortcut_after_dwell(generation))
                return
            if not self.shortcut_pending:
                raise RuntimeError("序列缺少下一步，已取消。")
            self.shortcut_feedback_wait_started = None
            self.shortcut_wait_deadline = None
            self.shortcut_active = self.shortcut_pending.pop(0)
            self._record_motion_event("sequence_dwell_finished", next_step=self.shortcut_active["number"])
            self._start_shortcut_step(self.shortcut_active)
        except Exception as exc:
            self._cancel_shortcut_queue(f"快捷流程中止 · {exc}")
            self._show_error(str(exc))

    def _handle_shortcut_failed(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        active = self.shortcut_active
        if active is None or int(active["generation"]) != int(payload["generation"]):
            return
        self._cancel_shortcut_queue(f"快捷流程中止 · {payload['reason']}")

    def _cancel_shortcut_queue(self, reason: str = "快捷流程已取消") -> None:
        timer = self.__dict__.get("shortcut_wait_timer")
        if timer is not None:
            self.after_cancel(timer)
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

    def _capture_user_zero_worker(self, node, axis):
        generation = self.cancel_generation
        self._reset_motion_cancel()
        self._check_motion_cancelled()
        status = self._read_statusword(node, fresh=True)
        if not self._operation_disabled(status):
            raise ValueError("请先 STOP，确认驱动器失能后再置零。")
        first = self._read_actual_position_inc(node, fresh=True)
        time.sleep(0.15)
        baseline = self._read_actual_position_inc(node, fresh=True)
        if abs(baseline - first) > 1:
            raise ValueError("位置仍在变化，未设置零点。")
        try:
            self._switch_on_for_position_mode(node, baseline)
            self._initialize_enable_hold(node, baseline)
            # Bit 8 requests Halt throughout Operation Enabled. Bit 4 (new setpoint)
            # stays clear. The first enabled sample may differ from the disabled latch.
            halt_enabled = 0x0F | BIT(8)
            self._motion_state(node, halt_enabled, 0x6F, 0x27)
            enabled = self._read_actual_position_inc(node, fresh=True)
            if int(node.sdo[0x6040].raw) != halt_enabled:
                raise RuntimeError("Halt 控制字未确认，未设置零点。")
            if int(node.sdo[0x6061].raw) != NodeOperationMode.PROFILE_POSITION.value:
                raise RuntimeError("位置模式未确认，未设置零点。")
            settle_until = time.monotonic() + 0.15
            while True:
                self._check_motion_cancelled()
                last = self._read_actual_position_inc(node, fresh=True)
                if abs(last - enabled) > 1 or self._read_statusword(node, fresh=True) & 0x6F != 0x27:
                    raise ValueError("使能采样期间位置或驱动状态发生变化，未设置零点。")
                remaining = settle_until - time.monotonic()
                if remaining <= 0:
                    break
                self.test_stop_requested.wait(min(0.025, remaining))
        finally:
            stopped = self._disable_drive_worker(only_node=node)[node.id]
        self._check_motion_cancelled()
        position = stopped["position_inc"]
        if (not self._operation_disabled(stopped["statusword"])
                or abs(position - enabled) > 1 or abs(position - last) > 1):
            raise ValueError("失能后的反馈与使能采样不一致，未设置零点。")
        self._post("log", f"置零采样 · 节点 {node.id} · 使能前读数 {baseline} → 使能实读 {enabled} "
                   f"→ 停稳 {position} 计数")
        self._post("user_zero", {"axis": axis, "node": node, "generation": generation,
                                 "raw_um": self._inc_to_um(position), "sample_time": time.monotonic()})

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
        self._run_stop_command("Halt", self._halt_worker)

    def stop_target_motion(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Stop target", self._halt_worker)

    def quick_stop(self) -> None:
        self._cancel_shortcut_queue("快捷流程已停止")
        self.test_stop_requested.set()
        self._run_stop_command("Quick stop", self._quick_stop_worker)

    def set_linked_zero(self) -> None:
        self._safe_start("Set pair zero", self._set_linked_zero_worker)

    def enable_drive(self) -> None:
        profile = self._profile_snapshot()
        def start():
            self._reset_motion_cancel()
            self._enable_drive_worker(profile)
        self._safe_start("Enable drive", start)

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
        self._run_stop_command("STOP", self._stop_and_disable_worker)

    def _stop_and_disable_worker(self) -> None:
        if not self.nodes:
            return
        self._disable_drive_worker()

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
                positions = [panel.position - panel.user_zero_um]
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
        self._run_stop_command("Stop cycle", self._quick_stop_worker)

    def _safe_start(self, label: str, func: Callable[[], object]) -> bool:
        try:
            self._run_command(label, func)
            return True
        except Exception as exc:
            self._log(f"{label} not started: {exc}")
            self._show_error(str(exc))
            return False

    def _run_command(self, label: str, func: Callable[[], object]) -> None:
        if self.__dict__.get("start_confirmation") is not None and label != "Disconnect":
            raise RuntimeError("请先继续或取消待确认的运动。")
        if label not in ("Disconnect", "Connect", "复核零点"):
            self._check_reference_ready()
        if label != "Disconnect" and (self.__dict__.get("close_in_progress", False)
                                      or self.__dict__.get("disconnect_in_progress", False)):
            raise RuntimeError("正在停止并断开设备，不能提交新命令。")
        if self.stop_pending:
            raise RuntimeError("正在确认停止，请等待。")
        if self.stop_unconfirmed and label not in ("Disconnect", "Connect"):
            raise RuntimeError("上次停止未确认，禁止新命令。请确认硬件已停止后断开并重新连接。")
        shortcut_running = self.__dict__.get("shortcut_active") is not None or bool(self.__dict__.get("shortcut_pending"))
        if shortcut_running and label != "Disconnect" and not label.startswith("快捷 "):
            raise RuntimeError("快捷流程进行中：请等待完成，或使用 STOP 停止后再操作。")
        if self.active_target_inc is not None and label != "Disconnect":
            raise RuntimeError("运动进行中：请等待到位，或使用 STOP 停止并确认失能后再操作。")
        if self.command_busy:
            raise RuntimeError(f"{label} 未提交：另一条命令仍在执行，请等待完成。")

        self.command_busy = True
        self.busy_var.set(label)
        # Lock immediately, not on the next 100 ms UI tick.
        for combo in self.__dict__.get("axis_combos", []):
            combo.configure(state="disabled")
        thread = threading.Thread(target=self._command_runner, args=(label, func, self.cancel_generation), daemon=True)
        thread.start()
        self._log(f"{label} 已提交后台执行。")

    def _run_stop_command(self, label: str, func: Callable[[], None]) -> None:
        self._dismiss_start_confirmation()
        if self.stop_pending:
            return
        if self.__dict__.get("close_in_progress") and label != "Close":
            return
        if not self.nodes:
            self.target_status_var.set("未连接设备 · 无法确认停止，请使用硬件停止")
            return
        self.stop_verified = False
        self.stop_pending = True
        self.cancel_generation += 1
        self.test_stop_requested.set()
        self.target_status_var.set("正在停止 · 等待设备确认")
        self._log(f"{label} requested.")
        thread = threading.Thread(target=self._stop_runner, args=(label, func), daemon=True)
        thread.start()
        self.stop_deadline_seconds = max(3.0, 2.0 + 1.5 * len(self.nodes))
        self.after(
            int(self.stop_deadline_seconds * 1000),
            lambda generation=self.cancel_generation: self._check_stop_deadline(generation),
        )

    def _check_stop_deadline(self, generation):
        # USB writes can block below the CAN library. Keep the UI independent
        # and never let an unreturned transport call look like a successful stop.
        if self.stop_pending and generation == self.cancel_generation:
            self.stop_unconfirmed = True
            self.target_status_var.set("停止超时未确认 · 请关闭驱动器电源")
            self._show_error(f"STOP 已请求，但 {self.__dict__.get('stop_deadline_seconds', 3.0):.1f} 秒内未确认停止。请关闭驱动器电源。")

    def _command_runner(self, label: str, func: Callable[[], object], generation=None) -> None:
        completion = ("busy", "Idle")
        try:
            with self.io_lock:
                if generation is not None and generation != self.cancel_generation:
                    raise RuntimeError("命令已被 STOP 取消")
                if label not in ("Disconnect", "Connect", "复核零点"):
                    self._check_reference_ready()
                self._motion_generation = generation
                result = func()
            if isinstance(result, StartConfirmation):
                completion = ("review_start", result)
            else:
                self._post("log", f"{label} complete.")
        except Exception as exc:
            error_handler(exc)
            self._post("log", f"{label} failed: {exc}")
            self._post("error", str(exc))
        finally:
            self._post(*completion)

    def _stop_runner(self, label: str, func: Callable[[], None]) -> None:
        try:
            # RPDO disable must not wait behind a long-running SDO/command lock.
            # Each send is serialized with motion writes; cancellation gates all
            # subsequent enable/setpoint writes, so a worker cannot undo STOP.
            for node in list(self.nodes.values()):
                try:
                    self._write_controlword(node, 0x00)
                except Exception as exc:
                    self._post("log", f"Immediate stop send failed: {exc}")
            if not self.io_lock.acquire(timeout=2.0):
                raise RuntimeError("通信线程未及时释放，停止状态未确认")
            try:
                # Every UI stop route uses the same verified disable path.
                self._stop_and_disable_worker()
                self.stop_verified = True
            finally:
                self.io_lock.release()
            self._post("log", f"{label} complete.")
        except Exception as exc:
            self.stop_unconfirmed = True
            self._post("target", "停止未确认 · 请关闭驱动器电源")
            self._post("log", f"{label} failed: {exc}")
            self._post("error", f"停止未确认，请关闭驱动器电源。\n{exc}")
        finally:
            self.stop_pending = False

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
            self._post("target", "零点复核通过 · 请重新选择运动")
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

    def _move_linked_absolute_worker(
        self,
        target_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        self._validate_target(target_um, limits)
        if bool(limits["enabled"]):
            self._apply_linked_limits_worker(limits, linked)
        axes = self._require_linked_axes(linked)
        starts = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        targets = self._linked_targets_for_logical_position(target_um, linked)
        distance_um = max(self._inc_to_um(abs(targets[axis] - starts[axis])) for axis in targets)
        timeout = self._manual_motion_timeout(distance_um, profile)
        result = self._run_linked_move(
            targets,
            profile,
            timeout,
            linked,
            starts_inc=starts,
            sync_mode="position",
            target_label=f"Linked target {target_um:.2f} um",
        )
        if result["status"] != "OK":
            raise RuntimeError(str(result["status"]))

    def _move_linked_relative_worker(
        self,
        delta_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        axes = self._require_linked_axes(linked)
        starts = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        delta_inc = self._um_to_inc(delta_um)
        targets = {
            axis: starts[axis] + direction * delta_inc
            for axis, _, direction in axes
        }

        offsets = dict(linked["offsets"])
        for axis, _, direction in axes:
            if axis in offsets:
                logical_target = direction * self._inc_to_um(targets[axis] - int(offsets[axis]))
                self._validate_target(logical_target, limits)
            else:
                self._validate_target(self._inc_to_um(targets[axis]), limits)
        if bool(limits["enabled"]) and all(axis in offsets for axis, _, _ in axes):
            self._apply_linked_limits_worker(limits, linked)

        result = self._run_linked_move(
            targets,
            profile,
            self._manual_motion_timeout(abs(delta_um), profile),
            linked,
            starts_inc=starts,
            sync_mode="travel",
            target_label=f"Linked relative {delta_um:+.2f} um",
        )
        if result["status"] != "OK":
            raise RuntimeError(str(result["status"]))

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
            raw = offset + user_target
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
                raise RuntimeError("使能后位置跨越分段边界，目标未发送，请复核位置。")

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

    def _run_linked_actuator_test_worker(
        self,
        test_config: dict[str, object],
        linked: dict[str, object],
    ) -> None:
        speeds = list(test_config["speeds"])
        distances = list(test_config["distances"])
        settle_time = float(test_config["settle_time"])
        step_timeout = float(test_config["step_timeout"])
        return_after = bool(test_config["return_after"])
        base_profile = dict(test_config["profile"])
        axes = self._require_linked_axes(linked)
        step_no = 1
        self._post("log", "Starting linked actuator test on A+B.")

        def run_step(speed: float, distance: float, number: int) -> dict[str, object]:
            profile = dict(base_profile)
            profile["velocity"] = speed
            profile["velocity_inc"] = self._um_to_inc(speed)
            if speed <= 0 or speed > float(profile["max_velocity"]):
                raise ValueError("双轴测试速度必须大于 0 且不能超过配置的最大速度。")
            if profile["velocity_inc"] < 1:
                raise ValueError("双轴测试速度量化后为 0 计数/秒，请提高速度或核对编码器型号。")
            starts = {
                axis: self._read_actual_position_inc(node, fresh=True)
                for axis, node, _ in axes
            }
            delta_inc = self._um_to_inc(distance)
            targets = {
                axis: starts[axis] + direction * delta_inc
                for axis, _, direction in axes
            }
            offsets = dict(linked["offsets"])
            for axis, _, direction in axes:
                if axis in offsets:
                    logical_target = direction * self._inc_to_um(targets[axis] - int(offsets[axis]))
                    self._validate_target(logical_target, dict(profile["limits"]))
                else:
                    self._validate_target(self._inc_to_um(targets[axis]), dict(profile["limits"]))
            result = self._run_linked_move(
                targets,
                profile,
                step_timeout,
                linked,
                starts_inc=starts,
                sync_mode="travel",
                target_label=f"Linked test step {number}",
            )
            for axis, values in dict(result["axes"]).items():
                self._post(
                    "test_result",
                    {
                        "step": number,
                        "axis": axis,
                        "speed": speed,
                        "command": float(values["command"]),
                        "actual": float(values["actual"]),
                        "error": float(values["error"]),
                        "duration": float(result["duration"]),
                        "status": result["status"],
                    },
                )
            return result

        for speed in speeds:
            for distance in distances:
                if self.test_stop_requested.is_set():
                    self._post("log", "Linked actuator test stopped before next step.")
                    return
                result = run_step(float(speed), float(distance), step_no)
                if result["status"] != "OK":
                    raise RuntimeError(str(result["status"]))
                step_no += 1
                if self.test_stop_requested.wait(settle_time):
                    return

                if return_after:
                    if self.test_stop_requested.is_set():
                        self._post("log", "Linked actuator test stopped before reverse step.")
                        return
                    result = run_step(float(speed), -float(distance), step_no)
                    if result["status"] != "OK":
                        raise RuntimeError(str(result["status"]))
                    step_no += 1
                    if self.test_stop_requested.wait(settle_time):
                        return

        self._post("log", "Linked actuator test complete.")

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

    def _run_linked_move(
        self,
        targets_inc: dict[str, int],
        profile: dict[str, float | int],
        step_timeout: float,
        linked: dict[str, object],
        *,
        starts_inc: Optional[dict[str, int]] = None,
        sync_mode: str = "travel",
        disable_after: bool = True,
        target_label: str = "Linked move",
        before_setpoint: Optional[Callable[[], None]] = None,
    ) -> dict[str, object]:
        axes = self._require_linked_axes(linked)
        nodes = [node for _, node, _ in axes]
        starts = dict(starts_inc or {})
        for axis, node, _ in axes:
            starts.setdefault(axis, self._read_actual_position_inc(node, fresh=True))
            if axis not in targets_inc:
                raise ValueError(f"Missing linked target for {axis}.")

        currents = dict(starts)
        statuswords: dict[str, int] = {}
        best_remaining = {
            axis: abs(int(targets_inc[axis]) - starts[axis])
            for axis, _, _ in axes
        }
        last_progress = {axis: time.monotonic() for axis, _, _ in axes}
        progress_samples_by_axis = {axis: [best_remaining[axis]] * 2 for axis, _, _ in axes}
        mismatch_since: Optional[float] = None
        status = "OK"
        start_time = time.monotonic()

        self._validate_linked_targets(targets_inc, linked)
        self.runtime_position_window_inc = max(1, int(profile.get("position_window_inc", self.__dict__.get("runtime_position_window_inc", 1))))
        self.active_target_inc = None
        self.active_start_inc = None

        if bool(dict(profile.get("limits", {})).get("enabled")):
            self._apply_linked_limits_worker(dict(profile["limits"]), linked)
        try:
            for axis, node, _ in axes:
                self._prepare_profile_position(node, profile, starts[axis])
            for axis, node, _ in axes:
                statuswords[axis] = self._read_statusword(node, fresh=True)

            if before_setpoint is not None:
                before_setpoint()
            self._command_profile_positions(
                [(node, int(targets_inc[axis])) for axis, node, _ in axes]
            )
            self._post("target", target_label)
            self._post(
                "target_start",
                " / ".join(f"{axis[-1]} {self._inc_to_um(starts[axis]):.2f}" for axis, _, _ in axes),
            )
            self._post("target_moved", "A 0.00 / B 0.00")
            self._post("target_progress", "0.0%")

            deadline = time.monotonic() + step_timeout
            while time.monotonic() < deadline:
                if self.test_stop_requested.is_set():
                    status = "Stopped"
                    break

                for axis, node, _ in axes:
                    try:
                        node.tpdo[3].wait_for_reception(timeout=0.02)
                    except Exception:
                        pass
                    currents[axis] = self._read_actual_position_inc(
                        node,
                        fallback=currents[axis],
                    )
                    statuswords[axis] = self._read_statusword(
                        node,
                        fallback=statuswords[axis],
                    )

                now = time.monotonic()
                remaining = {
                    axis: abs(int(targets_inc[axis]) - currents[axis])
                    for axis, _, _ in axes
                }
                travelled = {
                    axis: direction * self._inc_to_um(currents[axis] - starts[axis])
                    for axis, _, direction in axes
                }
                totals = {
                    axis: direction * self._inc_to_um(int(targets_inc[axis]) - starts[axis])
                    for axis, _, direction in axes
                }
                progress = [
                    100.0 if totals[axis] == 0 else travelled[axis] / totals[axis] * 100.0
                    for axis, _, _ in axes
                ]
                pair_error = self._linked_pair_error_um(
                    currents,
                    starts,
                    linked,
                    sync_mode,
                )
                self._post("target_moved", f"A {travelled['Actuator A']:.2f} / B {travelled['Actuator B']:.2f}")
                self._post("target_remaining", f"{max(self._inc_to_um(value) for value in remaining.values()):.2f}")
                self._post("target_progress", f"{max(0.0, min(100.0, min(progress))):.1f}%")
                self._post("linked_status", self._linked_status_payload(currents, linked, pair_error))

                fault_axis = next(
                    (axis for axis, _, _ in axes if statuswords[axis] & (BIT(3) | BIT(13))),
                    None,
                )
                if fault_axis is not None:
                    self._quick_stop_nodes(nodes)
                    fault = "drive fault" if statuswords[fault_axis] & BIT(3) else "following error"
                    status = f"{fault_axis} {fault}"
                    break

                stalled_axis = None
                for axis, _, _ in axes:
                    best_remaining[axis], last_progress[axis], stalled = self._update_progress_watchdog(
                        best_remaining[axis],
                        self._progress_sample(progress_samples_by_axis[axis], remaining[axis]),
                        last_progress[axis],
                        now,
                        max(1, self._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
                        MOTION_STALL_TIMEOUT_SECONDS,
                    )
                    if stalled:
                        stalled_axis = axis
                        break
                if stalled_axis is not None:
                    self._quick_stop_nodes(nodes)
                    status = f"{stalled_axis} stalled"
                    break

                if pair_error > float(linked["tolerance_um"]):
                    mismatch_since = mismatch_since or now
                    if now - mismatch_since >= PAIR_MISMATCH_TIMEOUT_SECONDS:
                        self._quick_stop_nodes(nodes)
                        status = f"Pair mismatch {pair_error:.2f} um"
                        break
                else:
                    mismatch_since = None

                reached = [
                    bool(statuswords[axis] & BIT(10))
                    and remaining[axis] <= max(1, self.runtime_position_window_inc)
                    for axis, _, _ in axes
                ]
                if all(reached):
                    break
                time.sleep(0.08)
            else:
                if self.test_stop_requested.is_set():
                    status = "Stopped"
                else:
                    self._quick_stop_nodes(nodes)
                    status = "Timeout"

            if self.test_stop_requested.is_set():
                status = "Stopped"
            else:
                for axis, node, _ in axes:
                    currents[axis] = self._read_actual_position_inc(node, fresh=True)
                    statuswords[axis] = self._read_statusword(node, fresh=True)
            if status == "OK":
                fault_axis = next(
                    (axis for axis, _, _ in axes if statuswords[axis] & (BIT(3) | BIT(13))),
                    None,
                )
                if fault_axis is not None:
                    raise RuntimeError(f"{fault_axis} fault during final verification")
                for axis, _, _ in axes:
                    remaining = abs(int(targets_inc[axis]) - currents[axis])
                    if not statuswords[axis] & BIT(10) or remaining > max(1, self.runtime_position_window_inc):
                        raise RuntimeError(
                            f"{axis} endpoint not confirmed: remaining {self._inc_to_um(remaining):.2f} um, "
                            f"status=0x{statuswords[axis]:04X}"
                        )
                final_pair_error = self._linked_pair_error_um(currents, starts, linked, sync_mode)
                if final_pair_error > float(linked["tolerance_um"]):
                    raise RuntimeError(f"Final pair mismatch {final_pair_error:.2f} um exceeds tolerance")
        except Exception as exc:
            status = f"Error: {exc}"
            if not self.test_stop_requested.is_set():
                try:
                    self._quick_stop_nodes(nodes)
                except Exception:
                    pass
        finally:
            # STOP owns verified cleanup after cancelling this worker. Repeating
            # multi-axis SDO verification here would keep io_lock from STOP.
            if not self.test_stop_requested.is_set() and (disable_after or status != "OK"):
                try:
                    disabled_result = self._disable_nodes(nodes)
                    if status == "OK":
                        for axis, node, _ in axes:
                            self._verify_disabled_target(node, int(targets_inc[axis]), disabled_result)
                except Exception as exc:
                    status = f"{status}; disable error: {exc}"

        pair_error = self._linked_pair_error_um(currents, starts, linked, sync_mode)
        axis_results = {}
        for axis, _, direction in axes:
            command_um = direction * self._inc_to_um(int(targets_inc[axis]) - starts[axis])
            actual_um = direction * self._inc_to_um(currents[axis] - starts[axis])
            axis_results[axis] = {
                "command": command_um,
                "actual": actual_um,
                "error": actual_um - command_um,
            }

        duration = time.monotonic() - start_time
        self._post("linked_status", self._linked_status_payload(currents, linked, pair_error))
        self._post("target", f"{target_label}: {status}")
        self._post(
            "log",
            f"{target_label} {status}; mismatch={pair_error:.2f} um, duration={duration:.2f} s.",
        )
        return {
            "status": status,
            "duration": duration,
            "pair_error": pair_error,
            "axes": axis_results,
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

    def _halt_worker(self) -> None:
        nodes = list(self.nodes.values()) or [self._require_node()]
        for node in nodes:
            self._write_controlword(node, 0x0F | BIT(8))
        if self.active_start_inc is not None:
            current_inc = self._read_actual_position_inc(self._require_node(), fresh=True)
            self._update_target_progress(current_inc)
        self.active_target_inc = None
        self.active_start_inc = None
        self._post("target", "Halted")

    def _quick_stop_worker(self) -> None:
        nodes = list(self.nodes.values()) or [self._require_node()]
        self._quick_stop_nodes(nodes)
        if self.active_start_inc is not None:
            current_inc = self._read_actual_position_inc(self._require_node(), fresh=True)
            self._update_target_progress(current_inc)
        self.active_target_inc = None
        self.active_start_inc = None
        self._post("target", "Quick stop")

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

    def _disable_drive_worker(self, *, only_node=None) -> dict[int, dict[str, int]]:
        try:
            nodes = [only_node] if only_node is not None else list(self.nodes.values()) or [self._require_node()]
            result = self._disable_nodes(nodes)
            self._record_motion_event("drive_disabled")
        except Exception as exc:
            self.stop_unconfirmed = True
            self._record_motion_event("disable_failed", error=str(exc))
            self._post("target", "停止未确认 · 请关闭驱动器电源")
            raise
        else:
            self.stop_unconfirmed = False
            if isinstance(result, dict):
                for axis, node in self.nodes.items():
                    if node.id in result:
                        count = int(result[node.id]["position_inc"])
                        self.__dict__.setdefault("reference_positions", {})[axis] = count
                        if axis == self._active_axis_name():
                            self.last_position_inc = count
            self._post("target", "已停止 · 已核对失能与位置稳定")
            return result
        finally:
            self.active_target_inc = None
            self.active_start_inc = None

    def _verify_disabled_target(self, node, target_inc: int, result: object) -> None:
        if not isinstance(result, dict):
            return
        stopped = result.get(node.id)
        if stopped is None:
            return
        remaining = abs(int(stopped["position_inc"]) - int(target_inc))
        if remaining > max(1, self.runtime_position_window_inc):
            raise RuntimeError(
                f"失能后终点偏离目标 {self._inc_to_um(remaining):.2f} µm，"
                f"status=0x{int(stopped['statusword']):04X}"
            )

    def _quick_stop_nodes(self, nodes: list[BaseNode402]) -> None:
        errors = []
        for node in nodes:
            try:
                self._write_controlword(node, 0x0B)
            except Exception as exc:
                errors.append(f"node {node.id}: {exc}")
        if errors:
            raise RuntimeError("Quick stop failed for " + "; ".join(errors))

    def _disable_nodes(self, nodes: list[BaseNode402]) -> dict[int, dict[str, int]]:
        errors = []
        results = {}
        # Disable every axis before waiting for any individual acknowledgement.
        for node in nodes:
            try:
                self._write_controlword(node, 0x00)
            except Exception as exc:
                self._post("log", f"Node {node.id}: RPDO disable failed: {exc}")
        for node in nodes:
            try:
                result = verify_disabled(node)
                results[node.id] = result
                self._record_motion_event("stop_verified", node_id=node.id, **result)
            except Exception as exc:
                errors.append(f"node {node.id}: {exc}")
        if errors:
            self.stop_unconfirmed = True
            raise RuntimeError("Drive disable failed for " + "; ".join(errors))
        return results

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

    def _apply_linked_limits_worker(
        self,
        limits_config: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        axes = self._require_linked_axes(linked)
        if not bool(limits_config["enabled"]):
            for _, node, _ in axes:
                limits = node.sdo["Software position limit"]
                limits[1].raw = 0
                limits[2].raw = 0
            return

        offsets = dict(linked["offsets"])
        logical_min = float(limits_config["min"])
        logical_max = float(limits_config["max"])
        low_inc, high_inc = self._limit_count_bounds(logical_min, logical_max)
        for axis, node, direction in axes:
            offset = int(offsets[axis])
            endpoints = [
                offset + direction * low_inc,
                offset + direction * high_inc,
            ]
            limits = node.sdo["Software position limit"]
            limits[1].raw = min(endpoints)
            limits[2].raw = max(endpoints)
        self._post(
            "log",
            f"Applied linked logical limits {logical_min:.2f} to {logical_max:.2f} um.",
        )

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
        enabled_inc = self._startup_position_sample(node, "使能刚确认", status, reference)
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
            (0x00, 0x4F, 0x40, "失能已确认"),
            (0x06, 0x6F, 0x21, "准备就绪"),
            (0x07, 0x6F, 0x23, "Switched On 已确认"),
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
            raise RuntimeError("启用前状态不符，已取消启用（必须处于 Switched on 状态）")
        hold_inc = self._startup_position_sample(node, "使能前：写入保持目标前", status, reference_inc)
        self._check_position_reference(reference_inc, hold_inc)
        self._write_target_position(node, hold_inc)
        readback_inc = int(node.sdo[0x607A].raw)
        self._check_motion_cancelled()
        if readback_inc != hold_inc:
            raise RuntimeError("启用前保持目标读回不一致，已取消启用")
        self._post("log", f"启用前保持当前位置：{hold_inc} 计数，目标读回已确认。")
        return hold_inc

    def _startup_position_sample(self, node, stage, status, reference):
        self._check_motion_cancelled()
        position = self._read_actual_position_inc(node, fresh=True)
        self._post("log", f"启动采样 · 节点 {node.id} · {stage} · 状态字 0x{status:04X} · "
                   f"0x6064 = {position} 计数（{self._inc_to_um(position):.2f} µm） · "
                   f"距启动基线 {position - reference:+d} 计数")
        return position

    @staticmethod
    def _operation_disabled(status):
        return (status & 0x4F) == 0x40 or (status & 0x6F) in (0x21, 0x23)

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
            self._post("log", f"启动采样 · 节点 {idle_node.id} · 状态切换前 · 状态字 0x{status:04X} · "
                       f"上次 {previous_inc} 计数 → 实读 {current_inc} 计数")
            if self._operation_disabled(status):
                self.last_position_inc = current_inc
                return
        self.test_stop_requested.set()
        self._record_motion_event("unexpected_displacement", previous_inc=previous_inc, current_inc=current_inc)
        stopped = self._disable_drive_worker()
        self._post("target", "检测到意外位移，已停止 · 请核对当前位置")
        raise StartupDisplacement(
            f"检测到意外位移：{self._inc_to_um(previous_inc):.2f} → {self._inc_to_um(current_inc):.2f} µm。"
            "已取消本次目标并失能，请核对实际位置后再启动。", previous_inc, stopped,
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

    def _check_motion_cancelled(self):
        self._check_reference_ready()
        generation = self.__dict__.get("_motion_generation")
        if ((generation is not None and generation != self.cancel_generation)
                or self.test_stop_requested.is_set() or self.__dict__.get("stop_unconfirmed", False)):
            raise RuntimeError("运动已取消或停止状态未确认")

    def _reset_motion_cancel(self):
        self._check_reference_ready()
        if self.stop_pending or self.stop_unconfirmed:
            raise RuntimeError("停止处理中或停止状态未确认，不能开始运动")
        self.test_stop_requested.clear()

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

    def _write_controlword(self, node: BaseNode402, value: int) -> None:
        with self.controlword_lock:
            if value & 0x04 and value & 0x08:
                self._check_motion_cancelled()
            set_controlword(node, value)

    def _write_target_position(self, node: BaseNode402, value: int) -> None:
        with self.controlword_lock:
            self._check_motion_cancelled()
            set_target_position(node, value)

    def _command_profile_position(self, node: BaseNode402, target_inc: int) -> None:
        self._command_profile_positions([(node, target_inc)])

    def _command_profile_positions(self, commands: list[tuple[BaseNode402, int]]) -> None:
        controlword = 0x0F
        nodes = [node for node, _ in commands]
        for node, _ in commands:
            self._write_controlword(node, controlword)
        # This controller retains bit 12 while idle after enabling (0x1637).
        # Follow the vendor's send-target / request / acknowledge / clear
        # sequence; requiring an idle clear edge prevents any motion.
        for node, target_inc in commands:
            self._write_target_position(node, target_inc)
        for node, _ in commands:
            self._write_controlword(node, controlword | BIT(4))
        self._wait_setpoint_ack(nodes, True)
        for node, _ in commands:
            self._write_controlword(node, controlword)

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
                    raise RuntimeError("目标握手时驱动故障或未使能，已取消运动")
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
        if (self.node is not None and not self.status_busy and not self.command_busy
                and not self.stop_pending and not self.stop_unconfirmed
                and time.monotonic() >= self.__dict__.get("next_status_poll_time", 0.0)
                and not self.__dict__.get("close_in_progress", False)
                and not self.__dict__.get("disconnect_in_progress", False)):
            self.status_busy = True
            thread = threading.Thread(target=self._status_worker, daemon=True)
            thread.start()
        self.after(POLL_INTERVAL_MS, self._poll_status)

    def _status_worker(self) -> None:
        try:
            with self.io_lock:
                feedback_axis = self._active_axis_name()
                node = self.nodes.get(feedback_axis, self.node)
                if node is None:
                    return
                self.node = node

                # A recently received PDO can still repeat an old position.
                # While idle, display the result of a new 0x6064 upload each poll.
                idle = self.active_target_inc is None
                if not idle:
                    try:
                        node.tpdo[3].wait_for_reception(timeout=0.05)
                    except Exception:
                        pass

                position_inc = self._read_actual_position_inc(node, fresh=idle)
                sample_time = time.monotonic()
                statusword = self._read_statusword(node)
                state = str(getattr(node, "state", "-"))
                mode = self._read_mode(node)
                self.last_position_inc = position_inc
                self.last_statusword = statusword

                status = {
                    "axis": feedback_axis,
                    "sample_time": sample_time,
                    "position_um": self._inc_to_um(position_inc),
                    "position_inc": position_inc,
                    "statusword": statusword,
                    "state": state,
                    "mode": mode,
                }

                sample_positions = {feedback_axis: position_inc}
                if "Actuator A" in self.nodes and "Actuator B" in self.nodes:
                    linked = {
                        "enabled": True,
                        "invert_b": bool(self.__dict__.get("invert_b_enabled", False)),
                        "tolerance_um": 0.0,
                        "offsets": dict(self.linked_zero_offsets_inc),
                    }
                    pair_currents = {
                        axis: position_inc if pair_node is node else self._read_actual_position_inc(pair_node, fresh=idle)
                        for axis, pair_node, _ in self._require_linked_axes(linked)
                    }
                    sample_positions = pair_currents
                    self._post("linked_status", self._linked_status_payload(pair_currents, linked))

                if self.active_target_inc is None and self.__dict__.get("shortcut_wait_deadline") is not None:
                    step = self.__dict__.get("shortcut_active")
                    if step is not None:
                        expected = self._um_to_inc(float(step["target"]))
                        window = max(1, self.runtime_position_window_inc)
                        if statusword & (BIT(3) | BIT(13)) or abs(position_inc - expected) > window:
                            current = self._read_actual_position_inc(node, fresh=True)
                            live_status = self._read_statusword(node, fresh=True)
                            if live_status & (BIT(3) | BIT(13)) or abs(current - expected) > window:
                                self.test_stop_requested.set()
                                self._disable_drive_worker()
                                reason = "停留期间驱动故障或位置偏离，已取消后续步骤"
                                self._post("target", reason)
                                self._post("log", reason)
                                self._post_shortcut_failure(reason)

                if self.active_target_inc is not None:
                    now = time.monotonic()
                    if self.active_start_inc is not None:
                        self._update_target_progress(position_inc)

                    remaining_inc = abs(position_inc - self.active_target_inc)
                    software_stall = False
                    candidate_best = self.active_best_remaining_inc
                    candidate_last = self.active_last_progress_time
                    progress_samples = self.__dict__.setdefault('active_progress_samples', [])
                    progress_remaining = self._progress_sample(progress_samples, remaining_inc)
                    if self.active_best_remaining_inc is not None:
                        (
                            candidate_best,
                            candidate_last,
                            software_stall,
                        ) = self._update_progress_watchdog(
                            self.active_best_remaining_inc,
                            progress_remaining,
                            self.active_last_progress_time,
                            now,
                            max(1, self._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
                            MOTION_STALL_TIMEOUT_SECONDS,
                        )

                    target_window = self.runtime_position_window_inc
                    target_reached = bool(statusword & BIT(10))
                    close_enough = remaining_inc <= max(1, target_window)
                    overshot = self._target_overshot(position_inc)
                    if overshot or software_stall or (target_reached and close_enough) or statusword & (BIT(3) | BIT(13)):
                        # Verify a terminal decision against the live device, never only a PDO cache.
                        position_inc = self._read_actual_position_inc(node, fresh=True)
                        statusword = self._read_statusword(node, fresh=True)
                        now = time.monotonic()
                        remaining_inc = abs(position_inc - self.active_target_inc)
                        (candidate_best, candidate_last,
                         software_stall) = self._update_progress_watchdog(
                            self.active_best_remaining_inc,
                            self._progress_sample(progress_samples, remaining_inc, replace_last=True),
                            self.active_last_progress_time, now,
                            max(1, self._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
                            MOTION_STALL_TIMEOUT_SECONDS,
                        )
                        target_reached = bool(statusword & BIT(10))
                        close_enough = remaining_inc <= max(1, target_window)
                        overshot = self._target_overshot(position_inc)
                        self.last_position_inc = position_inc
                        self._update_target_progress(position_inc)
                        status.update(position_um=self._inc_to_um(position_inc), position_inc=position_inc, statusword=statusword,
                                      sample_time=now)
                        self._record_motion_event("live_verification", position_inc=position_inc,
                                                  statusword=statusword, stalled=software_stall)

                    self.active_best_remaining_inc = candidate_best
                    self.active_last_progress_time = candidate_last
                    self._record_motion_event("sample", position_inc=position_inc, statusword=statusword,
                                              remaining_inc=self.active_target_inc - position_inc)
                    if statusword & (BIT(3) | BIT(13)):
                        reason = "驱动故障" if statusword & BIT(3) else "跟随误差保护"
                        self._record_motion_event("stop", reason=reason, position_inc=position_inc,
                                                  statusword=statusword)
                        self._disable_drive_worker()
                        self._post("target", reason)
                        self._post("log", f"{reason}，运动已停止并失能；status=0x{statusword:04X}。")
                        self._post_shortcut_failure(reason)
                    elif overshot:
                        self._record_motion_event("stop", reason="passed_target", position_inc=position_inc,
                                                  target_inc=self.active_target_inc, statusword=statusword)
                        self._disable_drive_worker()
                        self._post("target", "越过目标保护 · 序列已中止")
                        self._post("log", "实时读数已越过目标且超出到位窗口，已停止并失能；请检查控制器轨迹。")
                        self._post_shortcut_failure("越过目标保护")
                    elif target_reached and close_enough and (now - self.active_target_time) > 0.1:
                        reached_um = self._inc_to_um(position_inc)
                        self._record_motion_event("target_reached", position_inc=position_inc,
                                                  statusword=statusword)
                        target_inc = self.active_target_inc
                        disabled_result = self._disable_drive_worker()
                        self._verify_disabled_target(node, target_inc, disabled_result)
                        self._post("single_axis_target", ("已到达", reached_um))
                        self._post("target_remaining", "0.00")
                        self._post("target_progress", "100.0%")
                        self._post("log", f"Target reached at {reached_um:.2f} um.")
                        generation = self._shortcut_generation_token()
                        if generation is not None:
                            self._post("shortcut_done", generation)
                    elif software_stall:
                        remaining_um = self._inc_to_um(self.active_target_inc - position_inc)
                        self._record_motion_event("stop", reason="no_progress_live_confirmed",
                                                  position_inc=position_inc, statusword=statusword,
                                                  remaining_um=remaining_um)
                        self._disable_drive_worker()
                        self._post("target", "无进展保护（已实时复核）")
                        self._post(
                            "log",
                            f"Motion halted after {MOTION_STALL_TIMEOUT_SECONDS:.0f} s without target progress; "
                            f"remaining {remaining_um:.2f} um; live SDO confirmed, position "
                            f"{self._inc_to_um(position_inc):.2f} um, status 0x{statusword:04X}.",
                        )
                        self._post_shortcut_failure("无进展保护")
                    elif now >= self.active_target_deadline:
                        self._record_motion_event("stop", reason="timeout", position_inc=position_inc)
                        self._disable_drive_worker()
                        self._post("target", "Motion timeout")
                        self._post("log", "Motion halted and disabled: target timeout.")
                        self._post_shortcut_failure("运动超时")
                    else:
                        # Capture BEFORE any halt resets the controller's demand position.
                        # Terminal decisions above never wait for these additional reads.
                        self._record_controller_snapshot(node)

                self.reference_positions = sample_positions
                self._post("connection_health", True)
                self._post("status", status)
        except Exception as exc:
            self._post("log", f"Status read failed: {exc}")
            if self.__dict__.get("reference_review") is None:
                self.reference_review = dict(self.__dict__.get("reference_positions", {}))
            self.cancel_generation = self.__dict__.get("cancel_generation", 0) + 1
            self._post("feedback_stale", None)
            self._post("connection_health", False)
            with self.io_lock:
                self._record_motion_event("communication_fault", error=str(exc))
                self.test_stop_requested.set()
                stop_errors = []
                try:
                    self._disable_nodes(list(self.nodes.values()))
                except Exception as stop_exc:
                    stop_errors.append(str(stop_exc))
                self.stop_unconfirmed = bool(stop_errors)
                self.active_target_inc = None
                self.active_start_inc = None
                message = "通信异常：已请求停止并失能" if not stop_errors else "通信异常：停止状态未确认，请使用硬件停止"
                self._record_motion_event("communication_stop", confirmed=not stop_errors, errors=stop_errors)
                self._post("target", message)
                self._post("log", message)
                self._post("error", message)
                self._post_shortcut_failure(message)
        finally:
            # Reserve a real idle window after I/O, even when a read takes longer
            # than the poll timer. This prevents status traffic starving commands.
            self.next_status_poll_time = time.monotonic() + STATUS_POLL_REST_SECONDS
            self.status_busy = False

    def _update_target_progress(self, position_inc: int) -> None:
        if self.active_start_inc is None or self.active_target_inc is None:
            return

        moved_um = self._inc_to_um(position_inc - self.active_start_inc)
        remaining_um = self._inc_to_um(self.active_target_inc - position_inc)
        total_inc = self.active_target_inc - self.active_start_inc

        if total_inc:
            progress = ((position_inc - self.active_start_inc) / total_inc) * 100.0
            progress = max(0.0, min(100.0, progress))
        else:
            progress = 100.0

        self._post("target_moved", f"{moved_um:.2f}")
        self._post("target_remaining", f"{remaining_um:.2f}")
        self._post("target_progress", f"{progress:.1f}%")

    def _target_overshot(self, position_inc: int) -> bool:
        if self.active_target_inc is None or self.active_start_inc is None:
            return False
        distance = self.active_target_inc - self.active_start_inc
        if not distance:
            return False
        direction = 1 if distance > 0 else -1
        return direction * (position_inc - self.active_target_inc) > max(1, self.runtime_position_window_inc)

    def _read_actual_position_inc(
        self, node: BaseNode402, fresh: bool = False, fallback: Optional[int] = None
    ) -> int:
        if not fresh:
            try:
                tpdo = node.tpdo[3]
                if self._pdo_is_fresh(tpdo):
                    return int(tpdo["Position Actual Value"].raw)
            except Exception:
                pass
            # Never substitute a previous position when feedback has expired.
        return self._read_feedback_sdo(node, "Position Actual Value")

    def _read_statusword(
        self, node: BaseNode402, fresh: bool = False, fallback: Optional[int] = None
    ) -> int:
        if not fresh:
            try:
                tpdo = node.tpdo[3]
                if self._pdo_is_fresh(tpdo):
                    return int(tpdo["Statusword"].raw)
            except Exception:
                pass
        return self._read_feedback_sdo(node, "Statusword")

    def _read_feedback_sdo(self, node, name):
        try:
            return int(node.sdo[name].raw)
        except Exception:
            if self.__dict__.get("command_busy") and self.__dict__.get("reference_review") is None:
                self.reference_review = dict(self.__dict__.get("reference_positions", {}))
                self.test_stop_requested.set()
                self._post("command_feedback_fault", node)
            raise

    @staticmethod
    def _pdo_is_fresh(tpdo) -> bool:
        # python-can/slcan timestamps are wall-clock seconds, not monotonic time.
        timestamp = tpdo.timestamp
        return timestamp is not None and 0 <= time.time() - timestamp <= PDO_MAX_AGE_SECONDS

    def _start_motion_trace(self, node, start_inc, target_inc, profile) -> None:
        self.controller_trace_next_time = 0.0
        self.controller_trace_unsupported = set()
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XeryonControl" / "results"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.motion_trace_path = folder / (datetime.now().strftime("motion_%Y%m%d_%H%M%S_%f") + ".jsonl")
            self._record_motion_event("command", node=node.id, start_inc=start_inc, target_inc=target_inc,
                                      um_per_count=self._inc_to_um(1), profile=profile)
            if self.motion_trace_path:
                self._post("log", f"运动轨迹：{self.motion_trace_path}")
        except OSError as exc:
            self.motion_trace_path = None
            self._post("log", f"无法保存轨迹：{exc}")

    def _record_controller_snapshot(self, node) -> None:
        now = time.monotonic()
        if self.motion_trace_path is None or now < self.controller_trace_next_time:
            return
        self.controller_trace_next_time = now + CONTROLLER_TRACE_INTERVAL_SECONDS
        values = {"read_started_monotonic_s": now, "host_target_inc": self.active_target_inc}
        fields = (("demand_inc", 0x6062), ("actual_inc", 0x6064),
                  ("following_error_inc", 0x60F4), ("target_inc", 0x607A),
                  ("error_register", 0x1001), ("manufacturer_status", 0x1002))
        # Bound additional bus occupancy; never retry indefinitely or hide a lost link.
        timeout, retries = node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES
        try:
            node.sdo.MAX_RETRIES = 1
            for name, index in fields:
                if self.test_stop_requested.is_set():
                    values["interrupted_by_stop"] = True
                    break
                if index in self.controller_trace_unsupported:
                    continue
                budget = 0.5 - (time.monotonic() - now)
                if budget <= 0:
                    values["read_budget_exhausted"] = True
                    break
                node.sdo.RESPONSE_TIMEOUT = min(timeout, 0.25, budget)
                try:
                    values[name] = int(node.sdo[index].raw)
                except SdoAbortedError as exc:
                    # Unsupported firmware objects are explicit missing evidence.
                    if exc.code not in (0x06020000, 0x06090011):
                        raise
                    self.controller_trace_unsupported.add(index)
                    values[name + "_unavailable"] = str(exc)
        except Exception as exc:
            values["read_error"] = str(exc)
            raise  # Existing communication-fault handling stops and disables the drive.
        finally:
            node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES = timeout, retries
            values["read_finished_monotonic_s"] = time.monotonic()
            self._record_motion_event("controller_snapshot", **values)

    def _record_motion_event(self, event: str, **values) -> None:
        if self.motion_trace_path is None:
            return
        try:
            with self.motion_trace_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": datetime.now().astimezone().isoformat(),
                                         "monotonic_s": time.monotonic(), "event": event,
                                         **values}, ensure_ascii=False) + "\n")
        except (OSError, TypeError) as exc:
            self.motion_trace_path = None
            self._post("log", f"轨迹写入失败：{exc}")

    def _read_mode(self, node: BaseNode402) -> str:
        try:
            mode_value = int(node.sdo["Modes of Operation Display"].raw)
        except Exception:
            try:
                mode_value = int(node.sdo[0x6061].raw)
            except Exception:
                return "-"

        for mode in NodeOperationMode:
            if mode.value == mode_value:
                return mode.name
        return str(mode_value)

    @staticmethod
    def _update_progress_watchdog(
        best_remaining_inc: int,
        remaining_inc: int,
        last_progress_time: float,
        now: float,
        min_progress_inc: int,
        stall_timeout: float,
    ) -> tuple[int, float, bool]:
        if remaining_inc <= best_remaining_inc - min_progress_inc:
            return remaining_inc, now, False
        return best_remaining_inc, last_progress_time, now - last_progress_time >= stall_timeout

    @staticmethod
    def _progress_sample(history, remaining_inc, replace_last=False):
        # Filter only progress history. Arrival, overshoot, faults and limits
        # continue to use the unfiltered live position/status immediately.
        # One forward spike must not set an unreachable all-time best that
        # falsely stops a still-moving slow trajectory ten seconds later.
        if replace_last and history:
            history[-1] = remaining_inc
        else:
            history.append(remaining_inc)
        del history[:-3]
        return sorted(history)[len(history) // 2]

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
                raise ValueError("默认限位已启用：请先完成收回端点置零，再开始运动。")
            low = panel.user_zero_um + DEFAULT_WORK_MIN_UM
            high = panel.user_zero_um + DEFAULT_WORK_MAX_UM
            user_target = target_um - panel.user_zero_um

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

    def _linked_snapshot(self, require_zero: bool = False) -> dict[str, object]:
        enabled = self._linked_mode_enabled()
        invert_b = self.__dict__.get("invert_b_enabled")
        if invert_b is None:
            invert_b = bool(self.invert_b_var.get())
        snapshot: dict[str, object] = {
            "enabled": enabled,
            "invert_b": invert_b,
            "tolerance_um": 0.0,
            "offsets": dict(self.linked_zero_offsets_inc),
        }
        if not enabled:
            return snapshot

        tolerance = self.__dict__.get("pair_tolerance_um")
        if tolerance is None:
            tolerance = self._parse_positive_float(self.pair_tolerance_var, "maximum A/B mismatch")
        if not isfinite(tolerance) or tolerance <= 0:
            raise ValueError("双轴最大位置差必须为大于 0 的有限数值。")
        snapshot["tolerance_um"] = tolerance

        missing = [axis for axis in ("Actuator A", "Actuator B") if axis not in self.nodes]
        if missing:
            raise RuntimeError("A+B linked mode requires both Actuator A and Actuator B to be connected.")
        if require_zero and any(axis not in self.linked_zero_offsets_inc for axis in self.nodes):
            raise RuntimeError("Set Pair Zero before linked absolute moves or the Standard Cycle.")
        return snapshot

    def _require_linked_axes(
        self,
        linked: dict[str, object],
    ) -> list[tuple[str, BaseNode402, int]]:
        if not bool(linked["enabled"]):
            raise RuntimeError("A+B linked mode is not enabled.")
        try:
            node_a = self.nodes["Actuator A"]
            node_b = self.nodes["Actuator B"]
        except KeyError as exc:
            raise RuntimeError("A+B linked mode requires two connected actuators.") from exc
        return [
            ("Actuator A", node_a, 1),
            ("Actuator B", node_b, -1 if bool(linked["invert_b"]) else 1),
        ]

    def _set_linked_zero_worker(self) -> None:
        linked = self._linked_snapshot()
        axes = self._require_linked_axes(linked)
        positions = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        self.linked_zero_offsets_inc = positions
        linked["offsets"] = dict(positions)
        self._post("pair_zero_status", "Pair zero set")
        self._post("linked_status", self._linked_status_payload(positions, linked, 0.0))
        self._post(
            "log",
            "Linked software zero captured for A and B. Device home offsets were not changed.",
        )

    def _invalidate_linked_zero(self, reason: str) -> None:
        if not self.__dict__.get("linked_zero_offsets_inc", {}):
            return
        self.linked_zero_offsets_inc = {}
        self._post("pair_zero_status", "Not set")
        self._post("log", f"Linked software zero cleared after {reason}.")

    def _validate_linked_targets(self, targets_inc: dict[str, int], linked: dict[str, object]) -> None:
        for axis, _, _ in self._require_linked_axes(linked):
            travel = self.travel_ranges.get(axis)
            if travel is None or not travel.confirmed:
                raise ValueError(f"双轴 {axis} 缺少有效的机械行程映射，已拒绝联动目标。")
            target_inc = int(targets_inc[axis])
            travel.validate(
                self._inc_to_um(target_inc),
                target_inc,
                self._inc_to_um(1),
                axis,
            )

    def _linked_targets_for_logical_position(
        self,
        target_um: float,
        linked: dict[str, object],
    ) -> dict[str, int]:
        offsets = dict(linked["offsets"])
        target_inc = self._um_to_inc(target_um)
        return {
            axis: int(offsets[axis]) + direction * target_inc
            for axis, _, direction in self._require_linked_axes(linked)
        }

    def _read_linked_logical_positions(
        self,
        linked: dict[str, object],
        *,
        fresh: bool = False,
    ) -> dict[str, float]:
        offsets = dict(linked["offsets"])
        return {
            axis: direction
            * self._inc_to_um(self._read_actual_position_inc(node, fresh=fresh) - int(offsets[axis]))
            for axis, node, direction in self._require_linked_axes(linked)
        }

    def _linked_pair_error_um(
        self,
        currents: dict[str, int],
        starts: dict[str, int],
        linked: dict[str, object],
        sync_mode: str,
    ) -> float:
        axes = self._require_linked_axes(linked)
        if sync_mode == "position":
            offsets = dict(linked["offsets"])
            values = {
                axis: direction * self._inc_to_um(currents[axis] - int(offsets[axis]))
                for axis, _, direction in axes
            }
        else:
            values = {
                axis: direction * self._inc_to_um(currents[axis] - starts[axis])
                for axis, _, direction in axes
            }
        return abs(values["Actuator A"] - values["Actuator B"])

    def _linked_status_payload(
        self,
        currents: dict[str, int],
        linked: dict[str, object],
        pair_error: Optional[float] = None,
    ) -> dict[str, object]:
        axes = self._require_linked_axes(linked)
        offsets = dict(linked["offsets"])
        if all(axis in offsets for axis, _, _ in axes):
            positions = {
                axis: direction * self._inc_to_um(currents[axis] - int(offsets[axis]))
                for axis, _, direction in axes
            }
        else:
            positions = {
                axis: self._inc_to_um(currents[axis])
                for axis, _, _ in axes
            }
        return {
            "position_a": positions["Actuator A"],
            "position_b": positions["Actuator B"],
            "pair_error": (
                abs(positions["Actuator A"] - positions["Actuator B"])
                if pair_error is None and all(axis in offsets for axis, _, _ in axes)
                else pair_error
            ),
        }

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
                low = panel.user_zero_um + DEFAULT_WORK_MIN_UM
                high = panel.user_zero_um + DEFAULT_WORK_MAX_UM
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
        return int(round((value_um / UM_PER_MM) * self.inc_per_mm))

    def _inc_to_um(self, value_inc: float) -> float:
        return (float(value_inc) / self.inc_per_mm) * UM_PER_MM

    def _parse_float(self, variable: tk.StringVar, label: str) -> float:
        try:
            value = float(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {variable.get()!r}") from exc
        if not isfinite(value):
            raise ValueError(f"{label.capitalize()} must be a finite number.")
        return value

    def _parse_positive_float(self, variable: tk.StringVar, label: str) -> float:
        value = self._parse_float(variable, label)
        if value <= 0:
            raise ValueError(f"{label.capitalize()} must be greater than 0.")
        return value

    def _parse_nonnegative_float(self, variable: tk.StringVar, label: str) -> float:
        value = self._parse_float(variable, label)
        if value < 0:
            raise ValueError(f"{label.capitalize()} must be 0 or greater.")
        return value

    def _parse_int(self, variable: tk.StringVar, label: str) -> int:
        try:
            return int(variable.get().strip())
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {variable.get()!r}") from exc

    def _parse_positive_int(self, variable: tk.StringVar, label: str) -> int:
        value = self._parse_int(variable, label)
        if value <= 0:
            raise ValueError(f"{label.capitalize()} must be greater than 0.")
        return value

    def _parse_nonnegative_int(self, variable: tk.StringVar, label: str) -> int:
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
            self.after(10 if not self.ui_queue.empty() else 100, self._drain_ui_queue)

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
            self.encoder_combo.configure(state="disabled" if payload else "readonly")
        elif kind == "connection_health":
            healthy = bool(payload)
            self.connected_var.set("Connected" if healthy else "通信故障")
            self.header_connected_label.configure(
                style="HeaderMetricConnected.TLabel" if healthy else "HeaderMetricDisconnected.TLabel"
            )
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
            self.target_status_var.set(f"{label} · 用户位置 {raw-panel.user_zero_um:+.2f} µm"
                                       if panel.user_zero_set else f"{label} · 用户零点未设置")
        elif kind == "target_coordinate":
            self.target_position_var.set(f"{float(payload):.2f}")
        elif kind == "user_zero":
            if (payload["axis"] == self.active_axis_var.get() and self.node is payload["node"]
                    and payload["generation"] == self.cancel_generation and not self.test_stop_requested.is_set()
                    and not (self.stop_pending or self.stop_unconfirmed or self.close_in_progress
                             or self.disconnect_in_progress or self.reference_review is not None or self.reconnect_reason)):
                self.position_panel.apply_user_zero(payload["raw_um"], payload["sample_time"])
                self._log(f"已刷新编码器并确认失能停稳。收回端用户零点 = {payload['raw_um']:.4f} µm。")
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
            for combo in self.axis_combos:
                combo.configure(values=axes)
            if self.active_axis_var.get() not in axes:
                self.active_axis_var.set(axes[0])
        elif kind == "active_axis":
            self.active_axis_var.set(str(payload))
        elif kind == "clear_test_results":
            self.test_results.clear()
            for item in self.test_tree.get_children():
                self.test_tree.delete(item)
        elif kind == "test_result":
            self._append_test_result(payload)
        elif kind == "repeat_report":
            self.last_report_dir = str(payload["output_dir"])
            groups = payload["endpoints"]
            ranges = "; ".join(f"{k}: 峰峰值 {v['peak_to_peak_inc']} 计数 (n={v['n']})" for k, v in groups.items())
            self.repeat_status_var.set(f"{payload['status']} · {ranges} · 仅内部编码器重复性")


    def _refresh_ui_state(self) -> None:
        self.position_panel.redraw()
        self._update_workflow_preview()
        if not self.position_panel.is_fresh():
            self.position_var.set("-")
        motion_locked = self.command_busy or self.start_confirmation is not None or self.active_target_inc is not None
        for combo in self.axis_combos:
            combo.configure(state="disabled" if motion_locked else "readonly")
        linked_locked = (motion_locked
                         or self.shortcut_active is not None or bool(self.shortcut_pending) or self.stop_pending)
        for widget in (self.linked_mode_check, self.invert_b_check, self.set_pair_zero_button):
            widget.configure(state="disabled" if linked_locked else "normal")

    def _append_test_result(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self.test_results.append(dict(payload))

        values = (
            payload["step"],
            payload["axis"],
            f"{payload['speed']:.2f}",
            f"{payload['command']:.2f}",
            f"{payload['actual']:.2f}",
            f"{payload['error']:.2f}",
            f"{payload['duration']:.2f}",
            payload["status"],
        )
        status = str(payload["status"])
        if status == "OK":
            tags = ("ok",)
        elif status in {"Stopped", "Timeout"}:
            tags = ("warn",)
        else:
            tags = ("error",)
        self.test_tree.insert("", "end", values=values, tags=tags)

    def _set_target_progress_bar(self, value: str) -> None:
        try:
            progress = float(value.strip().rstrip("%"))
        except ValueError:
            progress = 0.0
        self.target_progress_bar_var.set(max(0.0, min(100.0, progress)))

    def _set_connected_state(self, connected: bool) -> None:
        self.connected_var.set("Connected" if connected else "Disconnected")
        if hasattr(self, "header_connected_label"):
            self.header_connected_label.configure(
                style="HeaderMetricConnected.TLabel" if connected else "HeaderMetricDisconnected.TLabel"
            )
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

    def _set_status(self, payload: object) -> None:
        if payload is None or isinstance(payload, dict):
            self.position_panel.accept_status(payload)
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
        # One Tk insertion/scroll per batch, instead of one repaint per line.
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "".join(f"[{timestamp}] {message}\n" for message in messages))
        lines = int(self.log_text.index("end-1c").split(".")[0]) - 1
        if lines > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{lines - LOG_MAX_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _center_notice(self, window: tk.Toplevel) -> None:
        # Let Tk measure the wrapped message and position the notice around its owner.
        self.tk.call("tk::PlaceWindow", window, "widget", self)
        window.lift()

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
        window = self.start_confirmation_window = tk.Toplevel(self)
        window.title("起点位置已变化")
        window.transient(self)
        old = self._inc_to_um(review.previous_inc)
        current = self._inc_to_um(review.stopped_inc)
        target = self._inc_to_um(self._um_to_inc(motion.target))
        relative = (f"相对用户零点：原起点 {old-motion.zero:+.2f} → 新起点 {current-motion.zero:+.2f} µm\n"
                    f"目标 {target-motion.zero:+.2f} µm" if motion.zero_set else "用户零点未设置")
        message = (f"{motion.axis} · {motion.label} · 已确认失能和位置稳定\n{relative}\n"
                   f"预计位移 {target-current:+.2f} µm，速度 {motion.profile['velocity']:g} µm/s。\n"
                   "是否从新起点继续？")
        if motion.zero_set and review.previous_inc == self._um_to_inc(motion.zero):
            message += "\n若推杆仍在刚标定的收回端点，请点「取消」，再点「收回端点置零」。"
        if original is not review:
            message = "设置已更新，请核对以下运动。\n" + message
        ttk.Label(window, text=message, padding=16, wraplength=460).pack()
        absolute = f"编码器绝对位置 {current:.2f} µm"
        if not motion.zero_set:
            absolute += f" · 编码器目标 {target:.2f} µm"
        ttk.Label(window, text=absolute, style="Muted.TLabel").pack(anchor="e", padx=12, pady=(0, 6))
        self._log(message.replace("\n", " "))
        self.target_status_var.set("位置已变化 · 等待确认")
        buttons = ttk.Frame(window, padding=(12, 0, 12, 12))
        buttons.pack()
        self.continue_button = ttk.Button(buttons, text="继续", command=lambda: self._continue_start(review))
        self.continue_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=self._dismiss_start_confirmation).pack(side="left", padx=4)
        window.protocol("WM_DELETE_WINDOW", self._dismiss_start_confirmation)
        if problem:
            self._block_start_confirmation(problem)
        self._center_notice(window)

    def _block_start_confirmation(self, problem):
        self.continue_button.state(["disabled"])
        ttk.Label(self.start_confirmation_window, text=problem, wraplength=440).pack(
            before=self.continue_button.master, padx=12, pady=(0, 10))
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
        self.start_confirmation_window.destroy()
        self.start_confirmation_window = None

    def _continue_start(self, review):
        if self.start_confirmation is not review or self.continue_button.instate(["disabled"]):
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

    def _show_error(self, message: str) -> None:
        # A modal dialog grabs all inputs and makes the main STOP unreachable.
        if self.close_in_progress:
            self._log(message)
            return
        window = self.__dict__.get("error_window")
        if window is not None and window.winfo_exists():
            self.error_message.set(message)
            self._center_notice(window)
            return
        self.error_window = window = tk.Toplevel(self)
        window.title("控制器提示（STOP / Esc 仍可用）")
        window.transient(self)
        self.error_message = tk.StringVar(value=message)
        ttk.Label(window, textvariable=self.error_message, wraplength=520, padding=20).pack()
        ttk.Button(window, text="停止 STOP", command=self.stop_actuator_test).pack(pady=6)
        ttk.Button(window, text="关闭提示", command=window.destroy).pack(pady=6)
        self._center_notice(window)

    def _on_close(self) -> None:
        if self.close_in_progress:
            if self.close_notice is not None:
                self._center_notice(self.close_notice)
            return
        self.close_in_progress = True
        self.close_cleanup_started = self.command_busy and self.busy_var.get() == "Disconnect"
        self.disconnect_in_progress = False  # Closing takes over any scheduled disconnect.
        self.close_deadline = time.monotonic() + max(5.0, 2.0 + 1.5 * len(self.nodes))
        window = self.__dict__.get("error_window")
        if window is not None and window.winfo_exists():
            window.destroy()
        self._cancel_shortcut_queue("快捷流程已关闭")
        self.test_stop_requested.set()
        self._finish_close()

    def _show_close_notice(self) -> None:
        if self.close_notice is not None:
            return
        self.close_notice = window = tk.Toplevel(self)
        window.title("等待控制器退出")
        window.transient(self)
        ttk.Label(window, wraplength=440, padding=16, text=(
            "设备停止确认或连接清理尚未完成。请确认所有连接轴的驱动器电源已关闭、"
            "推杆已停止，再手动退出。")).pack()
        ttk.Button(window, text="已确认断电并停止，退出软件",
                   command=self.destroy).pack(padx=12, pady=(0, 8))
        ttk.Button(window, text="隐藏提示", command=window.withdraw).pack(pady=(0, 10))
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self._center_notice(window)

    def _finish_close(self) -> None:
        if not self.close_in_progress:
            return
        if self.nodes and not self.close_stop_requested and not self.close_cleanup_started:
            self.close_stop_requested = True
            self._run_stop_command("Close", self._stop_and_disable_worker)

        busy = self.stop_pending or self.command_busy or self.__dict__.get("status_busy", False)
        if not busy and (self.stop_unconfirmed or (self.close_stop_requested and not self.stop_verified)):
            self._show_close_notice()
            return
        if not busy:
            if self.close_cleanup_started or self.network is None:
                self.destroy()
                return
            self.close_cleanup_started = True
            try:
                self._run_command("Disconnect", lambda: self._disconnect_worker(verify_stop=False))
            except Exception as exc:
                self._log(f"退出时提交断连失败：{exc}")
                self._show_close_notice()
                return
        if time.monotonic() >= self.close_deadline:
            self._show_close_notice()
        self.after(100, self._finish_close)


def main() -> None:
    app = XeryonControlGui()
    if "--smoke-test" in sys.argv:
        app.withdraw()
        app.update_idletasks()
        assert Path(app.eds_path_var.get()).is_file()
        workflow = app._workflow_snapshot()
        assert workflow["start"] == 50.0
        assert workflow["switch"] == 10050.0
        assert workflow["end"] == 13000.0
        assert app.node_id_var.get() == str(NODE_ID)
        assert app.default_limit_var.get() is True
        assert workflow["fast_speed"] == 50.0
        assert workflow["fast_acceleration"] == 500.0
        assert workflow["limits"] == {"enabled": True, "min": 50.0, "max": 13000.0}
        assert workflow["profile"]["position_window"] == 5.0
        assert workflow["profile"]["position_window_time"] == 200
        assert workflow["profile"]["following_error_window"] == 20.0
        assert workflow["profile"]["following_error_timeout"] == 1000
        assert app.node_b_enabled_var.get() is False
        assert app.linked_mode_var.get() is False
        assert app.test_return_var.get() is False
        assert app._update_progress_watchdog(100, 98, 1.0, 2.0, 1, 10.0) == (98, 2.0, False)
        assert app._update_progress_watchdog(98, 98, 2.0, 11.9, 1, 10.0) == (98, 2.0, False)
        assert app._update_progress_watchdog(98, 98, 2.0, 12.0, 1, 10.0) == (98, 2.0, True)
        raw_value = type("RawValue", (), {"raw": 123})()
        pdo_value = type("RawValue", (), {"raw": 456})()
        fake_pdo = type("FakePdo", (dict,), {"timestamp": None})(
            {"Position Actual Value": pdo_value, "Statusword": pdo_value}
        )
        fake_node = type(
            "FakeNode",
            (),
            {
                "sdo": {"Position Actual Value": raw_value, "Statusword": raw_value},
                "tpdo": {3: fake_pdo},
            },
        )()
        assert app._read_actual_position_inc(fake_node, fallback=999) == 123
        assert app._read_statusword(fake_node, fallback=888) == 123
        assert app._read_actual_position_inc(fake_node) == 123
        assert app._read_statusword(fake_node) == 123
        fake_pdo.timestamp = time.time()
        assert app._read_actual_position_inc(fake_node) == 456
        assert app._read_actual_position_inc(fake_node, fresh=True) == 123

        activation_order: list[object] = []
        original_write_profile = app._write_profile
        original_read_position = app._read_actual_position_inc
        original_motion_state = app._motion_state
        original_initialize_hold = app._initialize_enable_hold
        original_set_operation_mode = globals()["set_node_operation_mode"]
        original_wait_operation_mode = globals()["wait_for_node_operation_mode"]
        try:
            app._write_profile = lambda node, profile: activation_order.append("profile")
            app._read_actual_position_inc = lambda node, **kwargs: 0
            def simulate_state(node, word, mask, expected):
                activation_order.append(word)
                return expected
            app._motion_state = simulate_state
            def simulate_hold(node, reference):
                activation_order.append("hold verified")
                return reference
            app._initialize_enable_hold = simulate_hold
            globals()["set_node_operation_mode"] = (
                lambda node, mode, **kwargs: activation_order.append(mode)
            )
            globals()["wait_for_node_operation_mode"] = (
                lambda node, mode, **kwargs: activation_order.append("actual mode confirmed")
            )
            profile_node = type(
                "ProfileNode",
                (),
                {
                    "id": 32,
                    "sdo": {
                        "Statusword": type("RawValue", (), {"raw": 0x27})(),
                        "Position window": type("RawValue", (), {"raw": 0})(),
                        "Position window time": type("RawValue", (), {"raw": 0})(),
                    }
                },
            )()
            app._prepare_profile_position(
                profile_node,
                {"position_window_inc": 5, "position_window_time": 200},
            )
            assert activation_order == [
                "profile",
                0x00,
                0x06,
                0x07,
                NodeOperationMode.PROFILE_POSITION,
                "hold verified",
                0x0F,
                "actual mode confirmed",
            ]
        finally:
            app._write_profile = original_write_profile
            app._read_actual_position_inc = original_read_position
            app._motion_state = original_motion_state
            app._initialize_enable_hold = original_initialize_hold
            globals()["set_node_operation_mode"] = original_set_operation_mode
            globals()["wait_for_node_operation_mode"] = original_wait_operation_mode

        handshake: list[tuple[object, ...]] = []
        app._wait_setpoint_ack = lambda nodes, expected: None  # Protocol is tested separately.
        app._write_controlword = lambda node, value: handshake.append(("controlword", value))
        app._write_target_position = lambda node, value: handshake.append(("target", value))
        app._command_profile_position(fake_node, 789)
        assert handshake == [
            ("controlword", 0x0F),
            ("target", 789),
            ("controlword", 0x1F),
            ("controlword", 0x0F),
        ]

        node_a = object()
        node_b = object()
        labels = {node_a: "A", node_b: "B"}
        app.nodes = {"Actuator A": node_a, "Actuator B": node_b}
        pair_handshake: list[tuple[object, ...]] = []
        app._write_controlword = (
            lambda node, value: pair_handshake.append(("controlword", labels[node], value))
        )
        app._write_target_position = (
            lambda node, value: pair_handshake.append(("target", labels[node], value))
        )
        app._command_profile_positions([(node_a, 100), (node_b, 200)])
        assert pair_handshake[:4] == [
            ("controlword", "A", 0x0F),
            ("controlword", "B", 0x0F),
            ("target", "A", 100),
            ("target", "B", 200),
        ]
        assert pair_handshake[4:6] == [
            ("controlword", "A", 0x1F),
            ("controlword", "B", 0x1F),
        ]
        assert pair_handshake[-2:] == [
            ("controlword", "A", 0x0F),
            ("controlword", "B", 0x0F),
        ]

        linked = {
            "enabled": True,
            "invert_b": False,
            "tolerance_um": 20.0,
            "offsets": {"Actuator A": 1000, "Actuator B": 2000},
        }
        ten_um = app._um_to_inc(10.0)
        assert app._linked_targets_for_logical_position(10.0, linked) == {
            "Actuator A": 1000 + ten_um,
            "Actuator B": 2000 + ten_um,
        }
        starts = {"Actuator A": 1000, "Actuator B": 2000}
        currents = {"Actuator A": 1000 + ten_um, "Actuator B": 2000 + ten_um}
        assert app._linked_pair_error_um(currents, starts, linked, "travel") == 0.0
        linked["invert_b"] = True
        assert app._linked_targets_for_logical_position(10.0, linked) == {
            "Actuator A": 1000 + ten_um,
            "Actuator B": 2000 - ten_um,
        }
        app.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    main()
