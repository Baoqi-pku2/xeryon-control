"""Encoder-driven position view. This widget never sends hardware commands."""
from math import ceil, floor, isfinite
import time
import tkinter as tk
from tkinter import ttk
from ui_theme import RoundedCard
from travel_range import (
    DEFAULT_END_MARGIN_UM,
    DEFAULT_WORK_MAX_UM,
    DEFAULT_WORK_MIN_UM,
    DEFAULT_WORK_VIEW_MAX_UM,
    GEOMETRY_EPSILON_UM,
    MODEL,
    STROKE_UM,
    TravelRange,
)


SPEEDS = (3, 5, 10, 20, 50, 100, 300)
FEEDBACK_MAX_AGE = 2.0
FONT_UI = "Microsoft YaHei UI"
FONT_NUM = "Segoe UI"


class PositionPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, style="App.TFrame", padding=(0, 0, 0, 0))
        self.app = app
        self.position = None
        self.received_at = None
        self.feedback_axis = None
        self.initialized_axis = None
        self.encoder_view_center = 0.0
        self.view_min, self.view_max = -STROKE_UM / 2, STROKE_UM / 2
        # Both tabs edit the same intended target; drawing never commands motion.
        self.target = app.target_position_var
        self.user_zero_um = 0.0
        self.user_zero_set = False
        self.user_target = tk.StringVar(value=self.target.get())
        self._syncing_target = False
        self.zero_text = tk.StringVar()
        self.speed = tk.StringVar(value="5")
        self.span = tk.StringVar(value="1000")
        self.full_view = True
        self.center_coordinate = tk.StringVar(value="0.00")
        self.end_margin = tk.StringVar(value=f"{DEFAULT_END_MARGIN_UM:.0f}")
        self.center_checked = tk.BooleanVar(value=False)
        self.travel_text = tk.StringVar()
        self.end_distance_text = tk.StringVar()
        self.actual_text = tk.StringVar(value="— µm")
        self.feedback_text = tk.StringVar(value="未连接 · 等待编码器反馈")
        self.delta_text = tk.StringVar(value="—")
        self.readiness_text = tk.StringVar()
        self.calibration_badge_text = tk.StringVar(value="零点未标定")
        self.columnconfigure(0, weight=1, uniform="main_columns")
        self.columnconfigure(1, weight=1, uniform="main_columns")
        self._compact = False
        self.rowconfigure(1, weight=1)

        self.connection_card = RoundedCard(self, "设备连接与零点")
        self.connection_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.connection_card.heading.pack_forget()
        connection = self.connection_card.content
        connection.columnconfigure(0, weight=1)
        port_row = ttk.Frame(connection)
        port_row.grid(row=0, column=0, sticky="w")
        ttk.Label(port_row, text="串口").pack(side="left")
        port = ttk.Combobox(port_row, textvariable=app.channel_var, values=app.serial_ports, width=9)
        port.pack(side="left", padx=(8, 6))
        app.channel_combos.append(port)
        ttk.Button(port_row, text="刷新", command=app.refresh_ports).pack(side="left", padx=(0, 8))
        ttk.Button(port_row, text="连接设备", style="Accent.TButton", command=self.connect).pack(side="left")
        zero_row = ttk.Frame(connection)
        zero_row.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.zero_button = ttk.Button(zero_row, text="收回端点置零", style="Setup.TButton", command=app.set_current_user_zero)
        self.zero_button.pack(side="left", padx=(0, 10))
        self.calibration_badge = ttk.Label(zero_row, textvariable=self.calibration_badge_text, style="BadgeWarn.TLabel")
        self.calibration_badge.pack(side="left")
        self.zero_row = zero_row

        self.visual_card = RoundedCard(self, f"位置示意 · {MODEL}")
        self.visual_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        visual = self.visual_card.content
        visual.columnconfigure(0, weight=1)
        visual.rowconfigure(1, weight=1)
        visual_header = ttk.Frame(visual)
        visual_header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        visual_header.columnconfigure(0, weight=1)
        ttk.Label(visual_header, text="蓝色实际位置 · 橙色目标 · 向下伸出为正",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        view_tools = ttk.Frame(visual_header)
        view_tools.grid(row=1, column=0, sticky="w")
        ttk.Button(view_tools, text="整体视图", command=self.show_full_travel).pack(side="left", padx=(0, 6))
        ttk.Button(view_tools, text="放大当前位置", command=self.center_view).pack(side="left")

        self.canvas = tk.Canvas(visual, width=450, height=220, background="#EFEDF1", highlightthickness=0,
                                highlightbackground="#cbd5e1")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda event: self.redraw())
        self.canvas.bind("<Button-1>", self.drag_target)
        self.canvas.bind("<B1-Motion>", self.drag_target)

        telemetry = tk.Frame(visual, background="#EFEDF1", highlightbackground="#E6E4ED", highlightthickness=0, padx=12, pady=6)
        telemetry.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        telemetry.columnconfigure(0, weight=1)
        tk.Label(telemetry, textvariable=self.feedback_text, background="#EFEDF1", foreground="#22252D",
                 font=(FONT_UI, 9, "bold"), height=1, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(telemetry, textvariable=self.travel_text, background="#EFEDF1", foreground="#626B7B",
                 font=(FONT_UI, 9), wraplength=480, anchor="nw", justify="left").grid(
                     row=1, column=0, sticky="w", pady=(2, 0))
        tk.Label(telemetry, textvariable=self.end_distance_text, background="#EFEDF1", foreground="#626B7B",
                 font=(FONT_UI, 9), height=1, anchor="w").grid(row=2, column=0, sticky="w", pady=(2, 0))

        self.control_card = RoundedCard(self, "目标与运动")
        self.control_card.grid(row=1, column=1, sticky="nsew")
        # Keep live text from repeatedly changing the requested workstation size.
        # The grid expands both cards into available space; their contents stay native.
        for card in (self.visual_card, self.control_card):
            card.configure(width=510, height=480)
            card.pack_propagate(False)
        controls = self.control_card.content
        controls.columnconfigure(0, weight=1)

        readout_card = tk.Frame(controls, background="#EFEDF1", highlightbackground="#E6E4ED", highlightthickness=0, padx=12, pady=4)
        readout_card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        readout_card.columnconfigure(0, weight=1)
        self.actual_caption = tk.StringVar(value="当前位置 · 编码器 µm")
        ttk.Label(readout_card, textvariable=self.actual_caption, style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        value_row = tk.Frame(readout_card, background="#EFEDF1")
        value_row.grid(row=0, column=1, sticky="e")
        self.actual_value_label = tk.Label(
            value_row,
            textvariable=self.actual_text,
            font=(FONT_NUM, 22, "bold"),
            width=9,
            anchor="w",
            foreground="#155DCC",
            background="#EFEDF1",
        )
        self.actual_value_label.pack(side="left")
        tk.Label(
            value_row,
            text="µm",
            font=(FONT_UI, 12, "bold"),
            foreground="#64748b",
            background="#EFEDF1",
        ).pack(side="left", padx=(6, 0), pady=(7, 0))
        ttk.Label(readout_card, textvariable=self.zero_text, style="CardMuted.TLabel", wraplength=440).grid(row=1, column=0, columnspan=2, sticky="w")

        target_header = ttk.Frame(controls)
        target_header.grid(row=1, column=0, sticky="ew")
        target_header.columnconfigure(0, weight=1)
        ttk.Label(target_header, text="目标位置（µm）", foreground="#155DCC", font=(FONT_UI, 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(target_header, textvariable=self.delta_text, style="StepBadge.TLabel", anchor="center").grid(row=0, column=1, sticky="e")

        self.entry = ttk.Entry(controls, textvariable=self.user_target, font=(FONT_NUM, 14, "bold"))
        self.entry.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.scale = ttk.Scale(controls, from_=self.view_min, to=self.view_max, command=self.slide_target)
        self.scale.grid(row=3, column=0, sticky="ew", pady=(6, 2))
        self.scale.bind("<Left>", lambda event: self.nudge(-1))
        self.scale.bind("<Right>", lambda event: self.nudge(1))

        range_row = ttk.Frame(controls)
        range_row.grid(row=4, column=0, sticky="ew", pady=(0, 3))
        range_row.columnconfigure(0, weight=1)
        self.range_text = tk.StringVar()
        ttk.Label(range_row, textvariable=self.range_text, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(range_row, text="拖动目标不直接运动", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        speed_header = ttk.Frame(controls)
        speed_header.grid(row=5, column=0, sticky="ew")
        ttk.Label(speed_header, text="运动速度", font=(FONT_UI, 9, "bold")).pack(side="left")
        self.speed_entry = ttk.Entry(speed_header, textvariable=self.speed, width=9)
        self.speed_entry.pack(side="left", padx=6)
        ttk.Label(speed_header, text="µm/s（2–500）", style="Muted.TLabel").pack(side="left")

        speeds = ttk.Frame(controls)
        speeds.grid(row=6, column=0, sticky="ew", pady=(3, 4))
        for col in range(4):
            speeds.columnconfigure(col, weight=1)
        self.speed_buttons = []
        for i, (label, value) in enumerate((("慢速 · 5", 5), ("中速 · 20", 20), ("快速 · 50", 50), ("高速 · 300", 300))):
            button = ttk.Radiobutton(speeds, text=label, value=str(value), variable=self.speed, style="Speed.TRadiobutton")
            button.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 3, 0 if i == 3 else 3), pady=1)
            self.speed_buttons.append(button)

        encoder = tk.Frame(controls, background="#EFEDF1", highlightbackground="#E6E4ED", highlightthickness=0, padx=10, pady=3)
        encoder.grid(row=7, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(encoder, text="编码器分辨率：", style="CardMuted.TLabel").pack(side="left")
        ttk.Label(encoder, textvariable=app.encoder_var, style="StepBadge.TLabel").pack(side="left", padx=(2, 12))
        self.encoder_check = ttk.Checkbutton(encoder, text="已核对实物标签",
                                             variable=app.encoder_verified_var, command=self.redraw)
        self.encoder_check.pack(side="left")

        self.readiness_label = ttk.Label(controls, textvariable=self.readiness_text, wraplength=440,
                                         style="Status.TLabel", anchor="center")
        self.readiness_label.grid(row=8, column=0, sticky="ew", pady=(0, 4))

        buttons = ttk.Frame(controls)
        buttons.grid(row=9, column=0, sticky="ew")
        buttons.columnconfigure((0, 1), weight=1)
        self.start_button = ttk.Button(buttons, text="开始运动", style="Start.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(buttons, text="停止 STOP", style="Stop.TButton", command=app.stop_actuator_test).grid(row=0, column=1, sticky="ew")

        footer_state = tk.Frame(controls, background="#EFEDF1", highlightbackground="#E6E4ED", highlightthickness=0, padx=8, pady=4)
        footer_state.grid(row=10, column=0, sticky="ew", pady=(5, 0))
        footer_state.columnconfigure(1, weight=1)
        footer_state.columnconfigure(3, weight=1)
        ttk.Label(footer_state, text="执行：", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        tk.Label(footer_state, textvariable=app.target_status_var, background="#EFEDF1", foreground="#22252D",
                 font=(FONT_UI, 9, "bold"), wraplength=260, anchor="nw", justify="left").grid(
                     row=0, column=1, sticky="w")
        ttk.Label(footer_state, text="驱动：", style="CardMuted.TLabel").grid(row=0, column=2, sticky="w", padx=(6, 0))
        tk.Label(footer_state, textvariable=app.state_var, background="#EFEDF1", foreground="#626B7B",
                 font=(FONT_UI, 9), wraplength=120, anchor="nw", justify="left").grid(
                     row=0, column=3, sticky="w")

        self.shortcut_card = RoundedCard(self, "快捷流程")
        self.shortcut_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        shortcuts = self.shortcut_card.content
        self.shortcut_grid = ttk.Frame(shortcuts)
        self.shortcut_grid.pack(fill="x")
        self.shortcut_buttons = {}
        from shortcut_presets import SHORTCUT_PRESETS
        for number, name, target, speed in SHORTCUT_PRESETS:
            button = ttk.Button(self.shortcut_grid,
                text=f"{number}  {name}\n{target:g} µm · {speed:g} µm/s",
                style="Shortcut.TButton", command=lambda value=number: app.toggle_shortcut(value))
            self.shortcut_buttons[number] = button
        queue_row = ttk.Frame(shortcuts)
        queue_row.pack(fill="x", pady=(4, 0))
        self.queue_status = ttk.Label(queue_row, textvariable=app.shortcut_status_var,
                                     style="Muted.TLabel", wraplength=650)
        self.queue_status.pack(side="left", fill="x", expand=True)
        ttk.Label(queue_row, text="每步到位停留").pack(side="left")
        self.shortcut_dwell_entry = ttk.Entry(queue_row, textvariable=app.shortcut_dwell_var, width=7)
        self.shortcut_dwell_entry.pack(side="left", padx=8)
        ttk.Label(queue_row, text="秒后自动继续", style="Muted.TLabel").pack(side="left")
        self.clear_shortcut_button = ttk.Button(queue_row, text="清空选择", command=app.clear_shortcut_queue)
        self.clear_shortcut_button.pack(side="right")
        self.bind("<Configure>", self._responsive_layout)

        self.target.trace_add("write", self._raw_target_changed)
        self.user_target.trace_add("write", self._user_target_changed)
        app.active_axis_var.trace_add("write", self._axis_changed)
        self.center_coordinate.trace_add("write", lambda *args: self.invalidate_mapping())
        self.end_margin.trace_add("write", lambda *args: self.invalidate_mapping())
        self.redraw()

    def _responsive_layout(self, event):
        if event.widget is not self:
            return
        if event.width == getattr(self, "_layout_width", None):
            return
        self._layout_width = event.width
        # Fixed two-column workstation: size is bounded by the native window minimum.
        # Flow presets into measured columns. No clipped seven-button strip.
        width = max(1, event.width - 40)
        button_width = max(button.winfo_reqwidth() for button in self.shortcut_buttons.values()) + 8
        columns = max(1, min(7, width // button_width))
        for column in range(7):
            self.shortcut_grid.columnconfigure(column, weight=1 if column < columns else 0, uniform="presets" if column < columns else "")
        for index, button in enumerate(self.shortcut_buttons.values()):
            button.grid(row=index // columns, column=index % columns, sticky="ew", padx=3, pady=3)
        self.queue_status.configure(wraplength=max(240, width - 390))

    def _raw_target_changed(self, *args):
        if self._syncing_target:
            return
        self._syncing_target = True
        try:
            try:
                value = float(self.target.get())
                text = f"{value - self.user_zero_um:.2f}" if isfinite(value) else self.target.get()
            except ValueError:
                text = self.target.get()
            self.user_target.set(text)
        finally:
            self._syncing_target = False
        self.redraw()

    def _user_target_changed(self, *args):
        if self._syncing_target:
            return
        self._syncing_target = True
        try:
            try:
                value = float(self.user_target.get())
                text = repr(value + self.user_zero_um) if isfinite(value) else self.user_target.get()
            except ValueError:
                text = self.user_target.get()
            self.target.set(text)
        finally:
            self._syncing_target = False
        self.redraw()

    def apply_user_zero(self, raw_um, sample_time):
        raw_um = float(raw_um)
        axis = self.app.active_axis_var.get()
        travel = TravelRange()
        travel.confirm(raw_um + STROKE_UM / 2, DEFAULT_END_MARGIN_UM, axis)
        self.center_coordinate.set(f"{travel.center_um:.4f}")
        self.end_margin.set(f"{DEFAULT_END_MARGIN_UM:.0f}")
        self.center_checked.set(True)
        self.app.travel_range = travel
        self.app.travel_ranges[axis] = travel
        if not self.app.limit_enabled_var.get():
            safe_lo, safe_hi = travel.command_bounds
            self.app.min_limit_var.set(f"{safe_lo:.2f}")
            self.app.max_limit_var.set(f"{safe_hi:.2f}")
        self.user_zero_um = raw_um
        self.user_zero_set = True
        self.position = raw_um
        self.received_at = sample_time
        self.feedback_axis = axis
        self.encoder_view_center = raw_um
        self.target.set(repr(raw_um))
        self.show_full_travel()

    def clear_user_zero(self):
        self.user_zero_um = 0.0
        self.user_zero_set = False
        self.encoder_view_center = 0.0
        self.full_view = True
        self._raw_target_changed()

    def open_motion_setup(self):
        if self.editing_locked():
            self.app._show_error("请先等待当前运动结束或停止。")
            return
        existing = self.__dict__.get("setup_window")
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        window = self.setup_window = tk.Toplevel(self)
        window.title("运动前检查 · 不发送运动指令")
        window.transient(self.app)
        box = ttk.Frame(window, padding=20)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="机械行程检查", font=(FONT_UI, 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(box, text="只在本次连接生效；不会移动设备或修改设备零点。",
                  style="Muted.TLabel", wraplength=430).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Label(box, text="机械中位的编码器坐标（µm）").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(box, textvariable=self.center_coordinate, width=20).grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=6)
        ttk.Button(box, text="实物已在中位：使用当前位置", command=self.use_current_position_as_center).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(box, text="每端预留（µm）").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(box, textvariable=self.end_margin, width=20).grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=6)
        ttk.Checkbutton(box, text="已确认上述坐标对应实物机械中位", variable=self.center_checked,
                        command=self._mapping_check_changed).grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Label(box, text=f"用户 0 点不一定是机械中位。默认每端预留 {DEFAULT_END_MARGIN_UM:.0f} µm，厂家行程不代表安装机构的实际安全余量。",
                  style="Muted.TLabel", wraplength=430).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 12))
        actions = ttk.Frame(box)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew")
        actions.columnconfigure((0, 1), weight=1)
        ttk.Button(actions, text="确认并完成检查", style="Good.TButton", command=self.apply_motion_setup).grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(actions, text="取消", command=window.destroy).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        window.update_idletasks()
        x = self.app.winfo_rootx() + max(0, (self.app.winfo_width() - window.winfo_reqwidth()) // 2)
        y = self.app.winfo_rooty() + max(0, (self.app.winfo_height() - window.winfo_reqheight()) // 2)
        window.geometry(f"{window.winfo_reqwidth()}x{window.winfo_reqheight()}+{x}+{y}")

    def use_current_position_as_center(self):
        if not self.is_fresh():
            self.app._show_error("请等待有效的编码器位置反馈。")
            return
        self.center_coordinate.set(f"{self.position:.4f}")
        self.center_checked.set(True)
        self.redraw()

    def apply_motion_setup(self):
        self.apply_travel()
        if self.app.travel_range.confirmed:
            self.setup_window.destroy()

    def build_travel_settings(self, parent):
        box = ttk.LabelFrame(parent, text=f"{MODEL} · 机械行程与编码器坐标", padding=10)
        box.grid(row=0, column=0, sticky="ew", padx=12, pady=6)
        ttk.Label(box, text="杆长 95 mm；标称有效行程 60 mm（厂家公差 ±0.3 mm）。").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(box, text="机械中位对应的编码器坐标 (µm)").grid(row=1, column=0, sticky="w", pady=5)
        self.center_entry = ttk.Entry(box, textvariable=self.center_coordinate, width=18)
        self.center_entry.grid(row=1, column=1, padx=12)
        ttk.Label(box, text=f"每端预留 (µm)，默认 {DEFAULT_END_MARGIN_UM:.0f}").grid(row=2, column=0, sticky="w", pady=5)
        self.margin_entry = ttk.Entry(box, textvariable=self.end_margin, width=18)
        self.margin_entry.grid(row=2, column=1, padx=12)
        self.center_check = ttk.Checkbutton(box, text="已确认上述编码器坐标对应实际机械中位", variable=self.center_checked,
                                            command=self._mapping_check_changed)
        self.center_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self.default_limit_check = ttk.Checkbutton(
            box,
            text="默认限位：运行 0–13000 µm，主视图放大为 0–15000 µm",
            variable=self.app.default_limit_var,
            command=self.default_limit_changed,
        )
        self.default_limit_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        self.apply_travel_button = ttk.Button(box, text="应用行程映射（不运动）", command=self.apply_travel)
        self.apply_travel_button.grid(row=5, column=0, sticky="w", pady=4)
        ttk.Button(box, text="返回实时位置", command=lambda: self.app.notebook.select(0)).grid(row=5, column=1)
        ttk.Label(box, textvariable=self.travel_text, wraplength=900).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(box, text="取消默认限位后恢复 0–60000 µm 全行程显示和目标范围；机械端点预留仍然有效。\n"
                           "此处只设置本次连接的坐标映射和软件范围，不移动设备、不修改设备零点。\n"
                           "换轴、断开、通信失败或重新回零后需再次确认。不要用撞击端点的方法寻找零点。\n"
                           "厂家行程不是已安装机构的安全余量；下方可设置更窄的工作限位。", wraplength=900,
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=4)

    def _mapping_check_changed(self):
        if not self.center_checked.get():
            self.app.travel_range.invalidate()
        self.app.travel_ranges.pop(self.app.active_axis_var.get(), None)
        self.redraw()

    def invalidate_mapping(self):
        self.app.travel_range.invalidate()
        self.app.travel_ranges.pop(self.app.active_axis_var.get(), None)
        self.center_checked.set(False)
        self.redraw()

    def apply_travel(self):
        try:
            if self.editing_locked():
                raise ValueError("请先等待当前运动结束或停止。")
            if not self.is_fresh() or self.app.node is None:
                raise ValueError("请先连接设备并等待有效位置反馈。")
            if not self.center_checked.get():
                raise ValueError("请先确认机械中位的真实编码器坐标。")
            travel = TravelRange()
            travel.confirm(float(self.center_coordinate.get()), float(self.end_margin.get()), self.app.active_axis_var.get())
            lo, hi = travel.mechanical_bounds
            if not lo - GEOMETRY_EPSILON_UM <= self.position <= hi + GEOMETRY_EPSILON_UM:
                raise ValueError("当前编码器位置不在所填中位的 ±30 mm 内，请检查机械中位坐标。")
            low_inc, high_inc = travel.count_bounds(self.app._inc_to_um(1))
            if not -2**31 <= low_inc < high_inc <= 2**31-1:
                raise ValueError("坐标超出驱动器可表示的范围。")
            self.app.travel_range = travel
            self.app.travel_ranges[self.app.active_axis_var.get()] = travel
            if not self.app.limit_enabled_var.get():
                safe_lo, safe_hi = travel.command_bounds
                self.app.min_limit_var.set(f"{safe_lo:.2f}")
                self.app.max_limit_var.set(f"{safe_hi:.2f}")
            self.show_full_travel()
            self.app._log(f"已确认本次连接的机械行程：中位 {travel.center_um:.2f} µm，行程 60000 µm，"
                          f"每端预留 {travel.margin_um:.2f} µm；未发送运动指令。")
        except (ValueError, TypeError) as exc:
            self.app._show_error(str(exc))

    def default_limit_active(self):
        return (self.app.default_limit_enabled and not self.app.linked_mode_enabled
                and self.user_zero_set and self.app.travel_range.confirmed)

    def default_limit_changed(self):
        if self.editing_locked():
            self.app.default_limit_var.set(not self.app.default_limit_var.get())
            self.app._show_error("请先等待当前运动或快捷流程结束，再修改默认限位。")
            return
        try:
            if self.default_limit_active():
                target = float(self.target.get())
                lo, hi = self.target_bounds()
                self.target.set(f"{min(hi, max(lo, target)):.2f}")
            self.show_full_travel()
            message = ("已启用默认限位：目标 0–13000 µm，显示 0–15000 µm。"
                       if self.app.default_limit_enabled else "已取消默认限位：恢复完整行程显示和目标范围。")
            self.app._log(message + " 未发送运动指令。")
        except ValueError as exc:
            self.app._show_error(str(exc))

    def show_full_travel(self):
        self.full_view = True
        self.view_min, self.view_max = self.display_bounds()
        self.redraw()

    def display_bounds(self):
        if self.app.travel_range.confirmed:
            if self.default_limit_active():
                return self.user_zero_um + DEFAULT_WORK_MIN_UM, self.user_zero_um + DEFAULT_WORK_VIEW_MAX_UM
            return self.app.travel_range.mechanical_bounds
        if self.user_zero_set:
            # A nominal display span only; never used to authorize travel.
            return self.user_zero_um, self.user_zero_um + STROKE_UM
        return self.encoder_view_center - STROKE_UM / 2, self.encoder_view_center + STROKE_UM / 2

    def target_bounds(self):
        if self.app.travel_range.confirmed:
            um_per_count = self.app._inc_to_um(1)
            low_inc, high_inc = self.app.travel_range.count_bounds(um_per_count)
            if self.default_limit_active():
                low_inc = max(low_inc, ceil((self.user_zero_um + DEFAULT_WORK_MIN_UM) / um_per_count))
                high_inc = min(high_inc, floor((self.user_zero_um + DEFAULT_WORK_MAX_UM) / um_per_count))
            return self.app._inc_to_um(low_inc), self.app._inc_to_um(high_inc)
        # This is a preview range, not an assertion about mechanical endpoints.
        return self.display_bounds()

    def connect(self):
        try:
            self.app.connect_device()
        except Exception as exc:
            self.app._show_error(str(exc))

    def is_fresh(self):
        return (self.position is not None and self.received_at is not None
                and 0 <= time.monotonic() - self.received_at <= FEEDBACK_MAX_AGE
                and self.feedback_axis == self.app.active_axis_var.get())

    def _axis_changed(self, *args):
        axis = self.app.active_axis_var.get()
        self.app.last_position_inc = None
        self.app.last_statusword = None
        self.app.travel_range = self.app.travel_ranges.get(axis, TravelRange())
        self.position = self.received_at = self.feedback_axis = None
        self.initialized_axis = None
        self.clear_user_zero()
        self.redraw()

    def invalidate(self):
        self.app.travel_range.invalidate()
        self.app.travel_ranges.clear()
        self.center_checked.set(False)
        self.position = self.received_at = self.feedback_axis = None
        self.initialized_axis = None
        self.clear_user_zero()
        self.redraw()

    def accept_status(self, status):
        if status is None:
            self.app.travel_range.invalidate()
            self.app.travel_ranges.clear()
            self.center_checked.set(False)
            # Preserve the last reading, visibly marked stale. Never invent a
            # current position or erase the intended target on a transient error.
            self.received_at = None
            self.clear_user_zero()
            self.redraw()
            return
        if status.get("axis") != self.app.active_axis_var.get():
            return
        self.position = float(status["position_um"])
        self.received_at = status["sample_time"]
        self.feedback_axis = status["axis"]
        if self.initialized_axis != self.feedback_axis and self.is_fresh():
            self.initialized_axis = self.feedback_axis
            self.target.set(f"{self.position:.2f}")
            self.show_full_travel()
        self.redraw()

    def center_view(self):
        try:
            span = float(self.span.get())
            if not isfinite(span) or not 10 <= span <= STROKE_UM:
                raise ValueError("局部显示跨度需在 10–60000 µm 之间。")
            center = self.position if self.position is not None else self.encoder_view_center
            lo, hi = self.display_bounds()
            if self.default_limit_active():
                span = min(span, hi - lo)
            self.view_min = (max(lo, min(center - span / 2, hi - span))
                             if self.app.travel_range.confirmed else center - span / 2)
            self.view_max = self.view_min + span
            self.full_view = False
            self.redraw()
        except ValueError as exc:
            self.app._show_error(str(exc))

    def editing_locked(self):
        return (self.app.command_busy or self.app.active_target_inc is not None
                or self.app.shortcut_active is not None or bool(self.app.shortcut_pending)
                or self.app.stop_pending or self.app.stop_unconfirmed)

    def slide_target(self, value):
        if not self.editing_locked():
            self.target.set(f"{float(value):.2f}")

    def nudge(self, direction):
        if not self.editing_locked():
            try:
                value = float(self.target.get()) + direction * self.app._inc_to_um(1)
                lo, hi = self.target_bounds()
                self.target.set(f"{min(hi, max(lo, value)):.2f}")
            except ValueError:
                pass
        return "break"

    def drag_target(self, event):
        if not self.editing_locked():
            top, bottom = self.plot_vertical_bounds()
            fraction = min(1.0, max(0.0, (event.y - top) / (bottom - top)))
            value = self.view_min + fraction * (self.view_max - self.view_min)
            lo, hi = self.target_bounds()
            # In an uncalibrated zoom view, negative user coordinates remain
            # previewable; only a confirmed mechanical range constrains them.
            value = min(hi, max(lo, value)) if self.app.travel_range.confirmed else value
            self.target.set(f"{value:.2f}")

    def start(self):
        self.app.start_position_panel_move()
        self.redraw()

    def plot_vertical_bounds(self):
        # Leave room for the top instrument HUD and fixed body, and bottom status strip.
        return 96, max(140, max(200, self.canvas.winfo_height()) - 40)

    def redraw(self):
        fresh = self.is_fresh()
        locked = self.editing_locked()
        travel = self.app.travel_range
        for widget in (self.entry, self.scale, self.speed_entry, *self.speed_buttons):
            widget.configure(state="disabled" if locked else "normal")
        self.encoder_check.configure(state="disabled" if locked else "normal")
        verified = self.app.encoder_verified_var.get()
        linked = self.app.linked_mode_var.get()
        selected = set(self.app.shortcut_selected)
        active_number = (int(self.app.shortcut_active["number"])
                         if self.app.shortcut_active is not None else None)
        selectable = not locked and not self.app.stop_pending and not self.app.stop_unconfirmed
        for number, button in self.shortcut_buttons.items():
            style = ("ShortcutActive.TButton" if number == active_number else
                     "ShortcutSelected.TButton" if number in selected else "Shortcut.TButton")
            button.configure(style=style, state="normal" if selectable else "disabled")
        self.clear_shortcut_button.configure(
            state="normal" if self.app.shortcut_selected and selectable else "disabled")
        self.shortcut_dwell_entry.configure(state="disabled" if locked else "normal")
        default_ready = not self.app.default_limit_enabled or self.default_limit_active()
        ready = (fresh and self.app.node is not None and not locked and not linked and verified
                 and travel.confirmed and default_ready)
        if hasattr(self, "center_entry"):
            for widget in (self.center_entry, self.margin_entry, self.center_check,
                           self.default_limit_check, self.apply_travel_button):
                widget.configure(state="disabled" if locked else "normal")
        self.scale.configure(state="disabled" if locked else "normal")
        self.zero_button.configure(state="disabled" if locked or not fresh or linked or self.app.node is None else "normal")
        if travel.confirmed and self.user_zero_set:
            self.calibration_badge_text.set("默认限位 · 0–13 mm" if self.default_limit_active() else "已置零 · 0–60 mm")
            self.calibration_badge.configure(style="BadgeReady.TLabel")
        elif travel.confirmed:
            self.calibration_badge_text.set("行程已确认")
            self.calibration_badge.configure(style="BadgeReady.TLabel")
        else:
            self.calibration_badge_text.set("零点未标定")
            self.calibration_badge.configure(style="BadgeWarn.TLabel")

        if self.app.stop_unconfirmed:
            self.readiness_text.set("停止未确认 · 请关闭驱动器电源，再重新连接")
        elif self.app.stop_pending:
            self.readiness_text.set("正在停止 · 等待状态与位置确认")
        elif locked:
            self.readiness_text.set("命令执行中；STOP 可停止")
        elif self.app.node is None:
            self.readiness_text.set("第一步：连接设备")
        elif not fresh:
            self.readiness_text.set("等待有效位置反馈")
        elif linked:
            self.readiness_text.set("请在高级设置中关闭双轴联动")
        elif not verified:
            self.readiness_text.set("请勾选“已核对实物标签”")
        elif not default_ready:
            self.readiness_text.set("默认限位已启用 · 请先完成收回端点置零")
        elif not travel.confirmed:
            self.readiness_text.set("实物在收回端点时：点击“收回端点置零”")
        else:
            self.readiness_text.set("已就绪 · 设置目标后点击开始运动")
        can_submit = not locked and not self.app.stop_pending and not self.app.stop_unconfirmed
        self.start_button.configure(state="normal" if can_submit else "disabled")
        self.readiness_label.configure(style="StatusReady.TLabel" if ready else "StatusWarning.TLabel"
                                         if self.app.stop_unconfirmed else "Status.TLabel")
        self.actual_text.set(f"{self.position - self.user_zero_um:,.2f}" if self.position is not None else "—")
        self.app.user_position_var.set(f"{self.position - self.user_zero_um:.2f}" if fresh else "—")
        coordinate = "用户坐标" if self.user_zero_set else "编码器 · 物理零点未标定"
        self.actual_caption.set(f"当前位置 · {coordinate} µm" if fresh else "上次位置 · 反馈已过期 · µm" if self.position is not None else "当前位置 · 等待反馈 · µm")
        target_lo, target_hi = self.target_bounds()
        self.zero_text.set(f"0 = 收回端点；可设目标 {target_lo-self.user_zero_um:.0f}–{target_hi-self.user_zero_um:.0f} µm" if self.user_zero_set and travel.confirmed else "实物在收回端尽头时置零；此操作不移动设备")
        self.actual_value_label.configure(foreground="#155DCC" if fresh else "#64748b")
        if fresh:
            self.feedback_text.set(f"编码器反馈 · {time.monotonic() - self.received_at:.1f} s 前更新")
        elif self.position is not None:
            self.feedback_text.set("反馈已过期 · 当前实际位置未确认")
        else:
            self.feedback_text.set("等待编码器反馈 · 当前实际位置未确认")
        if self.full_view:
            self.view_min, self.view_max = self.display_bounds()
        slider_lo, slider_hi = target_lo, target_hi
        self.scale.configure(from_=slider_lo, to=slider_hi)
        self.range_text.set(("可设目标" if travel.confirmed else "目标预览") + f"：{(slider_lo-self.user_zero_um)/1000:+.2f} 至 {(slider_hi-self.user_zero_um)/1000:+.2f} mm")
        self.travel_text.set("默认限位：目标 0–13 mm · 主视图 0–15 mm · 端点保护保留"
                             if self.default_limit_active() else f"0 = 收回端点 · 标称行程 60 mm · 两端各留 {travel.margin_um/1000:g} mm"
                             if travel.confirmed and self.user_zero_set else f"中位坐标 {travel.center_um:.2f} µm · 每端预留 {travel.margin_um/1000:g} mm"
                             if travel.confirmed else "物理零点未标定；图中按固定编码器 0 坐标显示，不再自动居中")
        lo, hi = travel.mechanical_bounds
        if fresh and travel.confirmed:
            self.end_distance_text.set(f"距收回端 {max(0, self.position-lo)/1000:.3f} mm  · 距另一端 {max(0, hi-self.position)/1000:.3f} mm（按当前行程设置估算）")
        else:
            self.end_distance_text.set("机械端点未确认；预览范围不是可用行程，拖动目标不会启动运动" if not travel.confirmed else "等待有效反馈")
        try:
            target = float(self.target.get())
            if not isfinite(target):
                raise ValueError()
        except ValueError:
            target = None
        if target is None and not self.app.stop_pending and not self.app.stop_unconfirmed:
            self.readiness_text.set("请输入有效的目标坐标；点击开始可查看原因")
        elif travel.confirmed and not self.app.stop_pending and not self.app.stop_unconfirmed:
            try:
                travel.validate(target, self.app._um_to_inc(target), self.app._inc_to_um(1), self.app.active_axis_var.get())
                self.app._validate_default_limit(target)
            except ValueError:
                self.readiness_text.set("目标超出范围；点击开始查看详情")
        self.delta_text.set(f"待移动  {target - self.position:+,.2f} µm" if fresh and target is not None else "待移动  —")
        # Set the thumb variable directly: Scale.set would invoke its command.
        if target is not None:
            self.scale.configure(value=min(slider_hi, max(slider_lo, target)))

        c = self.canvas
        c.delete("all")
        w, h = max(460, c.winfo_width()), max(220, c.winfo_height())
        top, bottom = self.plot_vertical_bounds()
        ruler_x = max(86, round(w * 0.18))
        track_x1, track_x2 = ruler_x + 12, ruler_x + 22
        x = max(ruler_x + 206, round(w * 0.59))

        def y(value):
            return top + (value - self.view_min) / (self.view_max - self.view_min) * (bottom - top)

        # Top HUD Bar & Bottom Status Strip
        c.create_rectangle(0, 0, w, 30, fill="#f1f5f9", outline="")
        c.create_line(0, 30, w, 30, fill="#cbd5e1")
        c.create_rectangle(0, h - 28, w, h, fill="#f1f5f9", outline="")
        c.create_line(0, h - 28, w, h - 28, fill="#e2e8f0")

        hud_left = "● 用户坐标系 (0 = 收回端点)" if self.user_zero_set else "○ 编码器原始坐标 (零点待标定)"
        c.create_text(14, 15, text=hud_left, anchor="w", fill="#0f766e" if self.user_zero_set else "#626B7B", font=(FONT_UI, 9, "bold"))
        span_mm = (self.view_max - self.view_min) / 1000
        if actual_y := (min(bottom, max(top, y(self.position))) if self.position is not None else None):
            c.create_text(w - 14, 15, text=f"显示跨度 {span_mm:.1f} mm", anchor="e", fill="#64748b", font=(FONT_UI, 9))
        else:
            c.create_text(w - 14, 15, text="等待编码器位置反馈", anchor="e", fill="#626B7B", font=(FONT_UI, 9, "bold"))

        # Subtle background grid & scale spine
        for i in range(5):
            yy_grid = top + (bottom - top) * i / 4
            c.create_line(ruler_x, yy_grid, w - 18, yy_grid, fill="#e2e8f0")
        c.create_line(ruler_x, top, ruler_x, bottom, fill="#64748b", width=1)
        c.create_text(ruler_x - 10, top - 18, text="mm", anchor="e", fill="#64748b", font=(FONT_NUM, 9, "bold"))

        for i in range(17):
            value = self.view_min + (self.view_max - self.view_min) * i / 16
            yy = y(value)
            if i % 4 == 0:
                c.create_line(ruler_x - 8, yy, ruler_x, yy, fill="#334155", width=2)
                reference = self.user_zero_um
                tick = (value - reference) / 1000
                c.create_text(
                    ruler_x - 12,
                    yy,
                    text=f"{0.0 if abs(tick) < .0005 else tick:+.3f}",
                    anchor="e",
                    fill="#1e293b",
                    font=(FONT_NUM, 10, "bold"),
                )
            elif i % 2 == 0:
                c.create_line(ruler_x - 5, yy, ruler_x, yy, fill="#64748b")
            else:
                c.create_line(ruler_x - 3, yy, ruler_x, yy, fill="#94a3b8")

        # Continuous stroke corridor bar alongside the ruler
        c.create_rectangle(track_x1, top, track_x2, bottom, fill="#e2e8f0", outline="#cbd5e1")
        safe_lo, safe_hi = travel.command_bounds
        if travel.confirmed:
            ys_lo = max(top, min(bottom, y(safe_lo)))
            ys_hi = max(top, min(bottom, y(safe_hi)))
            c.create_rectangle(track_x1, min(ys_lo, ys_hi), track_x2, max(ys_lo, ys_hi), fill="#dbeafe", outline="#93c5fd")

        # The lower rod tip moves downward for positive extension from the fixed body.
        body_y = min(bottom, max(top, y(self.user_zero_um))) if self.user_zero_set else top

        # Guide centerline for push-rod travel
        c.create_line(x, top, x, bottom, fill="#cbd5e1", dash=(3, 3))

        if actual_y is not None:
            if abs(actual_y - body_y) > 0.5:
                c.create_rectangle(
                    track_x1 + 2,
                    min(body_y, actual_y),
                    track_x2 - 2,
                    max(body_y, actual_y),
                    fill="#3b82f6" if fresh else "#94a3b8",
                    outline="",
                )
            c.create_rectangle(x - 8, body_y - 24, x + 8, actual_y, fill="#cbd5e1", outline="#626B7B", tags="rod")
            c.create_line(x - 3, body_y - 20, x - 3, actual_y - 3, fill="#EFEDF1", width=3)
            c.create_line(x + 4, body_y - 20, x + 4, actual_y - 3, fill="#64748b", width=2)
            c.create_rectangle(x + 9, actual_y - 6, x + 20, actual_y + 2, fill="#334155", outline="#1e293b")
        else:
            c.create_rectangle(x - 7, body_y, x + 7, min(bottom, body_y + 78), outline="#94a3b8", dash=(4, 3))

        for a, b in (((lo, safe_lo), (safe_hi, hi)) if travel.confirmed else ()):
            ya, yb = max(top, min(bottom, y(a))), max(top, min(bottom, y(b)))
            if abs(ya - yb) > 0.5:
                c.create_rectangle(track_x1, min(ya, yb), track_x2, max(ya, yb), fill="#f59e0b", outline="#d97706")
                c.create_rectangle(x - 16, min(ya, yb), x + 16, max(ya, yb), fill="#d97706", outline="")

        if self.user_zero_set and self.view_min <= self.user_zero_um <= self.view_max:
            yy = y(self.user_zero_um)
            c.create_line(ruler_x, yy, x + 26, yy, fill="#0f766e", width=1, dash=(4, 3), tags="photo_zero")
            badge_y = max(46, body_y - 48)
            c.create_rectangle(x - 130, badge_y - 10, x - 12, badge_y + 10, fill="#ccfbf1", outline="#5eead4")
            c.create_text(x - 71, badge_y, text="收回端点 · 0 µm", fill="#0f766e", font=(FONT_UI, 9, "bold"))

        # Precision XLA-5-95 Actuator Body Housing
        pcb = "#0f766e" if fresh else "#64748b"
        c.create_rectangle(x - 160, body_y - 34, x + 18, body_y + 34, fill="#cbd5e1", outline="#626B7B", width=2, tags="body")
        c.create_line(x - 158, body_y - 32, x + 16, body_y - 32, fill="#EFEDF1")
        c.create_rectangle(x - 152, body_y - 29, x + 12, body_y + 29, fill=pcb, outline="#115e59" if fresh else "#626B7B")
        for dx, dy in ((-138, -21), (-138, 21), (-12, -21), (-12, 21)):
            c.create_oval(x + dx - 4, body_y + dy - 4, x + dx + 4, body_y + dy + 4, fill="#e2e8f0", outline="#334155")
            c.create_oval(x + dx - 2, body_y + dy - 2, x + dx + 2, body_y + dy + 2, fill="#22252D", outline="")
        for dx in (-112, -82):
            c.create_rectangle(x + dx, body_y - 21, x + dx + 18, body_y - 7, fill="#1e293b", outline="#94a3b8")
            c.create_rectangle(x + dx, body_y + 7, x + dx + 18, body_y + 21, fill="#1e293b", outline="#94a3b8")
        c.create_text(x - 95, body_y, text="XLA-5-95", fill="#99f6e4" if fresh else "#e2e8f0", font=(FONT_NUM, 7, "bold"))
        c.create_rectangle(x - 40, body_y - 21, x - 26, body_y + 21, fill="#facc15", outline="#a16207")
        c.create_rectangle(x - 174, body_y - 14, x - 160, body_y + 14, fill="#e2e8f0", outline="#64748b")
        c.create_rectangle(x - 12, body_y + 34, x + 12, body_y + 39, fill="#334155", outline="#1e293b")

        target_below = target is not None and actual_y is not None and y(target) > actual_y + 1
        if target is not None:
            yy = min(bottom, max(top, y(target)))
            if actual_y is not None and abs(yy - actual_y) > 16:
                vx = track_x2 + 12
                c.create_line(vx, actual_y, vx, yy, fill="#d97706", width=2, arrow=tk.LAST)
            c.create_rectangle(x - 14, yy - 5, x + 14, yy + 5, outline="#d97706", width=2, dash=(5, 3), tags="target")
            c.create_line(track_x2, yy, x + 26, yy, fill="#d97706", width=1, dash=(5, 3))
            in_view_t = self.view_min - 1e-6 <= target <= self.view_max + 1e-6
            target_mm = (target - self.user_zero_um) / 1000
            ty = max(44, min(h - 42, yy + (16 if target_below else -16)))
            c.create_rectangle(x + 28, ty - 10, min(w - 8, x + 150), ty + 10, fill="#fffbeb", outline="#fcd34d")
            c.create_text(
                x + 34,
                ty,
                text=f"目标 {target_mm:+.2f} mm" if in_view_t else "目标 · 超出视图",
                anchor="w",
                fill="#b45309",
                font=(FONT_UI, 9, "bold"),
            )
        if actual_y is not None:
            color = "#2477F5" if fresh else "#94a3b8"
            c.create_rectangle(
                x - 20,
                actual_y - 5,
                x + 20,
                actual_y + 5,
                fill=color,
                outline="#155DCC" if fresh else "#64748b",
                tags="actual" if fresh else "actual_stale",
            )
            c.create_line(track_x2, actual_y, x - 20, actual_y, fill=color, width=1)
            in_view_a = self.view_min - 1e-6 <= self.position <= self.view_max + 1e-6
            actual_mm = (self.position - self.user_zero_um) / 1000
            ay = max(44, min(h - 42, actual_y + (-16 if target_below else 16)))
            c.create_rectangle(
                x + 28,
                ay - 10,
                min(w - 8, x + 150),
                ay + 10,
                fill="#eff6ff" if fresh else "#f1f5f9",
                outline="#93c5fd" if fresh else "#cbd5e1",
            )
            c.create_text(
                x + 34,
                ay,
                text=(f"当前 {actual_mm:+.2f} mm" if fresh else "上次 · 已过期")
                + ("" if in_view_a else " · 超出视图"),
                anchor="w",
                fill="#155DCC" if fresh else "#64748b",
                font=(FONT_UI, 9, "bold"),
            )

        axis_label = "用户坐标 / mm" if self.user_zero_set else "编码器坐标 / mm"
        c.create_text(
            w / 2,
            h - 14,
            text=axis_label + (" · 局部放大" if not self.full_view else " · 已确认行程" if travel.confirmed else " · 姿态示意 / 端点未标定"),
            fill="#626B7B",
            font=(FONT_UI, 9, "bold"),
        )
