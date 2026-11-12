"""Advanced device, motion, calibration and reporting pages."""

from PySide6.QtCore import QSignalBlocker, Qt, QSize
from PySide6.QtWidgets import (
    QWidget,
    QTabWidget,
    QFrame, QBoxLayout, QGridLayout,
    QVBoxLayout,
    QFormLayout,
    QTableWidget,
    QTableWidgetItem,
    QPlainTextEdit,
    QHeaderView,
    QProgressBar,
)
from xeryon_control.ui.icons import icon
from xeryon_control.hardware.parameters import CANBitrate
from xeryon_control.ui.travel_readout import TravelReadout
from xeryon_control.ui.widgets import (
    button,
    check,
    combo,
    edit,
    label,
    row,
    scroll,
    value_label, glyph, divider,
)


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(16, 14, 16, 16)
        self.outer.setSpacing(12)
        self.columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.columns.setSpacing(12)
        self.outer.addLayout(self.columns)
        self.lanes = []
        for _ in range(2):
            lane = QVBoxLayout()
            lane.setSpacing(12)
            lane.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.columns.addLayout(lane, 1)
            self.lanes.append(lane)
        self.outer.addStretch()

    def resizeEvent(self, event):
        direction = (QBoxLayout.Direction.LeftToRight if event.size().width() >= 960
                     else QBoxLayout.Direction.TopToBottom)
        self.columns.setDirection(direction)
        super().resizeEvent(event)


class AdvancedPages(QTabWidget):
    def __init__(self, session, show_console, guarded):
        super().__init__()
        self.session = s = session
        m = s.position_panel
        self.guarded = guarded
        self.locked_widgets = []
        self.linked_widgets = []

        self.setObjectName("settingsTabs")
        self.setIconSize(QSize(18, 18))

        def page(name, symbol):
            widget = SettingsPage()
            self.addTab(scroll(widget), icon(symbol), name)
            return widget

        def group(page, title, symbol, column=0):
            box = QFrame()
            box.setObjectName("card")
            page.lanes[column].addWidget(box)
            layout = QVBoxLayout(box)
            layout.setContentsMargins(16, 13, 16, 15)
            layout.setSpacing(12)
            layout.addLayout(row(glyph(symbol), label(title, "title"), None))
            layout.addWidget(divider())
            form = QFormLayout()
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(8)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            layout.addLayout(form)
            return form

        def fields(form, items, readonly=False):
            if len(items) == 1 and not readonly:
                name, field = items[0]
                form.addRow(label(name, "settingLabel"), edit(field))
                return
            block = QFrame()
            if readonly:
                block.setObjectName("settingsMetrics")
            grid = QGridLayout(block)
            margin = 9 if readonly else 0
            grid.setContentsMargins(margin, margin, margin, margin)
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(4)
            index = 0
            for name, field in items:
                wide = field is s.eds_path_var or field is s.workflow_preview_var or field is s.target_status_var
                if wide and index % 2:
                    index += 1
                row_index, column = divmod(index, 2)
                span = 2 if wide else 1
                title = label(name, "muted")
                control = value_label(field, "settingValue", True) if readonly else edit(field)
                title.setBuddy(control)
                grid.addWidget(title, 3 * row_index, column, 1, span)
                grid.addWidget(control, 3 * row_index + 1, column, 1, span)
                grid.setRowMinimumHeight(3 * row_index + 2, 4)
                index += span
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            form.addRow(block)

        def actions(form, items):
            grid = QGridLayout()
            grid.setSpacing(6)
            for index, (name, action) in enumerate(items):
                role = "advancedStop" if "STOP" in name else "connect" if index == 0 else None
                item = button(name, lambda f=action: guarded(f), role)
                grid.addWidget(item, index // 3, index % 3)
            form.addRow(grid)

        quick = page("高级控制", "console")
        form = group(quick, "分段进退", "sequence", 0)
        fields(
            form,
            [
                ("工作起点 (µm)", s.workflow_start_var),
                ("快慢切换点 (µm)", s.workflow_switch_var),
                ("推进终点 (µm)", s.workflow_end_var),
                ("快速段速度 (µm/s)", s.workflow_fast_speed_var),
                ("慢速段速度 (µm/s)", s.workflow_slow_speed_var),
                ("快速段加速度 (µm/s²)", s.workflow_fast_acceleration_var),
                ("行程上限 (µm)", s.workflow_travel_limit_var),
                ("超时 (s)", s.workflow_timeout_var),
            ],
        )
        fields(
            form,
            [("阶段", s.workflow_status_var), ("分段预览", s.workflow_preview_var)],
            True,
        )
        actions(
            form,
            [
                ("分段推进", lambda: s.run_standard_cycle(False)),
                ("分段退出", lambda: s.run_standard_cycle(True)),
                ("STOP", s.stop_standard_cycle),
            ],
        )
        form = group(quick, "手动运动", "position", 1)
        form.addRow(label("单轴使用用户坐标，双轴使用联动坐标。", "muted"))
        fields(
            form,
            [
                ("本轴用户位置 (µm)", s.user_position_var),
                ("起点 (µm)", s.target_start_var),
                ("已移动 (µm)", s.target_moved_var),
                ("剩余距离 (µm)", s.target_remaining_var),
                ("进度", s.target_progress_var),
            ],
            True,
        )
        progress = QProgressBar()
        s.target_progress_bar_var.subscribe(
            lambda: progress.setValue(round(s.target_progress_bar_var.get()))
        )
        form.addRow(progress)
        fields(
            form, [("目标位置 (µm)", m.user_target), ("速度 (µm/s)", s.velocity_var)]
        )
        actions(form, [("移动到目标位置", s.move_to_target), ("STOP", s.quick_stop)])
        fields(form, [("相对距离 (µm)", s.relative_distance_var)])
        actions(
            form,
            [
                ("正向 +", lambda: s.move_relative(1)),
                ("负向 −", lambda: s.move_relative(-1)),
            ],
        )
        fields(form, [("点动步长 (µm)", s.jog_step_var)])
        actions(
            form,
            [
                ("点动 +", lambda: s.jog(1)),
                ("点动 −", lambda: s.jog(-1)),
                ("寻找参考点", s.home_device),
                ("移到工作起点", s.move_to_work_start),
                ("开启驱动", s.enable_drive),
                ("关闭驱动", s.disable_drive),
            ],
        )
        form = group(quick, "双轴联动", "settings", 0)
        self.linked_widgets = [
            check("双轴联动", s.linked_mode_var),
            check("反转 B 方向", s.invert_b_var),
            button("设置联动零点", lambda: guarded(s.set_linked_zero)),
        ]
        form.addRow(row(*self.linked_widgets, None))
        fields(form, [("最大 A/B 偏差 (µm)", s.pair_tolerance_var)])
        fields(
            form,
            [
                ("联动零点", s.pair_zero_status_var),
                ("A 位置 (µm)", s.position_a_var),
                ("B 位置 (µm)", s.position_b_var),
                ("A/B 偏差 (µm)", s.pair_error_var),
            ],
            True,
        )
        device = page("设备连接", "port")
        form = group(device, "连接设置", "port", 0)
        fields(form, [("CAN 接口", s.interface_var)])
        self.port = combo(s.channel_var, s.serial_ports, True)
        self.refresh_button = button("刷新串口", s.refresh_device, "quiet")
        self.refresh_button.setIcon(icon("refresh"))
        form.addRow("串口", row(self.port, self.refresh_button, None))
        self.refresh_hint = label("刷新位置前请松手，驱动可能短暂开启。", "muted")
        form.addRow(self.refresh_hint)
        form.addRow(
            "波特率", combo(s.bitrate_var, [str(int(v)) for v in CANBitrate], True)
        )
        fields(
            form,
            [
                ("节点 A ID", s.node_id_var),
                ("节点 B ID", s.node_b_id_var),
                ("EDS 文件", s.eds_path_var),
                ("启动超时 (s)", s.boot_timeout_var),
                ("扫描节点上限", s.probe_limit_var),
            ],
        )
        form.addRow(
            row(
                check("开启 B 轴", s.node_b_enabled_var),
                check("连接时复位", s.reset_on_connect_var),
                None,
            )
        )
        self.encoder = combo(s.encoder_var, ["10MU", "1MU", "250NAN", "100NAN"])
        form.addRow("编码器型号（连接前选择）", self.encoder)
        actions(
            form,
            [
                ("连接设备", s.connect_device),
                ("断开连接", s.disconnect_device),
                ("读取参数", s.read_device_settings),
                ("扫描节点", s.probe_node_ids),
                ("扫描波特率", s.probe_bitrates),
            ],
        )
        form = group(device, "实时状态", "pulse", 1)
        self.axis = combo(s.active_axis_var, s.available_axes)
        form.addRow("活动轴", self.axis)
        fields(
            form,
            [
                ("连接", s.connected_var),
                ("命令", s.busy_var),
                ("编码器绝对位置 (µm)", s.position_var),
                ("目标", s.target_status_var),
                ("CiA-402 状态", s.state_var),
                ("模式", s.mode_var),
                ("状态字", s.statusword_var),
                ("A 位置", s.position_a_var),
                ("B 位置", s.position_b_var),
                ("双轴偏差", s.pair_error_var),
            ],
            True,
        )
        form.addRow(
            label(
                "软件 STOP 通过 CANopen 执行。保留硬件 STO、电源开关和端点保护。",
                "muted",
            )
        )
        profile = page("运动参数", "settings")
        form = group(profile, "运动参数（仅本次运行）", "settings", 0)
        fields(
            form,
            [
                ("速度 (µm/s)", s.velocity_var),
                ("加速度 (µm/s²)", s.acceleration_var),
                ("最大速度 (µm/s)", s.max_velocity_var),
                ("最大加速度 (µm/s²)", s.max_acceleration_var),
                ("快速停止减速度 (µm/s²)", s.quick_stop_decel_var),
                ("到位窗口 (µm)", s.position_window_var),
                ("窗口保持时间 (ms)", s.position_window_time_var),
                ("跟随误差窗口 (µm)", s.following_error_window_var),
                ("跟随误差超时 (ms)", s.following_error_timeout_var),
            ],
        )
        actions(form, [("应用运动参数", s.apply_motion_profile)])
        form.addRow(label("加速度过渡约 0.1 秒", "muted"))
        form = group(profile, "回零参数", "position", 1)
        form.addRow(
            "方向", combo(s.home_direction_var, ["Positive index", "Negative index"])
        )
        fields(
            form, [("偏移 (µm)", s.home_offset_var), ("超时 (s)", s.home_timeout_var)]
        )


        tests = page("重复定位 / 测试", "sequence")
        form = group(tests, "功能测试", "console", 0)
        fields(
            form,
            [
                ("速度列表 (µm/s)", s.test_speeds_var),
                ("距离列表 (µm)", s.test_distances_var),
                ("间隔 (s)", s.test_settle_var),
                ("单步超时 (s)", s.test_timeout_var),
            ],
        )
        form.addRow(check("每步完成后反向移动", s.test_return_var))
        actions(
            form,
            [
                ("运行功能测试", s.run_actuator_test),
                ("停止并关闭驱动 STOP", s.stop_actuator_test),
                ("导出 CSV", s.export_test_results),
            ],
        )
        form = group(tests, "重复定位", "position", 1)
        fields(
            form,
            [
                ("重复次数（2–20）", s.repeat_cycles_var),
                ("端点间距（计数）", s.repeat_span_var),
                ("速度（计数/秒）", s.repeat_speed_var),
            ],
        )
        actions(
            form,
            [
                ("开始重复定位", s.start_repeatability),
                ("打开结果文件夹", s.open_report_folder),
            ],
        )
        form.addRow(value_label(s.repeat_status_var, "muted", True))
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "步骤",
                "轴",
                "速度 µm/s",
                "目标 µm",
                "实际 µm",
                "误差 µm",
                "时间 s",
                "状态",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(150)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        tests.outer.insertWidget(1, self.table, 1)
        limits = page("行程 / 零点", "position")
        form = group(limits, "行程标定", "position", 0)
        for name, field in [
            ("机械中位编码器坐标 (µm)", m.center_coordinate),
            ("每端预留 (µm)", m.end_margin),
            ("局部视图跨度 (µm)", m.span),
        ]:
            w = edit(field)
            form.addRow(name, w)
            self.locked_widgets.append(w)
        for w in [
            check("确认实物位于机械中位", m.center_checked, m._mapping_check_changed),
            check(
                "开启默认工作限位 0–13 mm", s.default_limit_var, m.default_limit_changed
            ),
            button("使用当前位置作为中位", m.use_current_position_as_center),
            button("确认行程映射（不运动）", m.apply_travel),
        ]:
            form.addRow(w)
            self.locked_widgets.append(w)
        group(limits, "行程概览", "pulse", 1).addRow(TravelReadout(m.travel_readings))
        form.addRow(
            label("请核对实物中位及安装余量。", "muted")
        )
        actions(form, [("返回实时位置", show_console)])
        form = group(limits, "工作区间（编码器坐标）", "settings", 1)
        form.addRow(check("开启更窄的工作区间", s.limit_enabled_var))
        fields(
            form,
            [("最小坐标 (µm)", s.min_limit_var), ("最大坐标 (µm)", s.max_limit_var)],
        )
        actions(
            form,
            [
                ("写入设备临时限位", s.apply_limits),
                ("清除设备临时限位", s.clear_limits),
            ],
        )

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setObjectName("deviceLog")
        self.addTab(self.log, icon("pulse"), "运行日志")

    def refresh(self):
        s = self.session
        locked = (
            s.command_busy
            or s.start_confirmation is not None
            or s.active_target_inc is not None
        )
        self.refresh_button.setText("刷新位置" if s.network is not None else "刷新串口")
        self.refresh_hint.setVisible(s.network is not None)
        self.refresh_button.setEnabled(not s.position_panel.editing_locked()
                                      and not (s.disconnect_in_progress or s.close_in_progress))
        self.encoder.setEnabled(
            s.connected_var.get() == "Disconnected" and s.network is None and not locked
        )
        self.axis.setEnabled(not locked)
        for w in self.linked_widgets:
            w.setEnabled(
                not (
                    locked or s.shortcut_active or s.shortcut_pending or s.stop_pending
                )
            )
        for w in self.locked_widgets:
            w.setEnabled(not s.position_panel.editing_locked())
        for w, choices, field in (
            (self.axis, s.available_axes, s.active_axis_var),
            (self.port, s.serial_ports, s.channel_var),
        ):
            if [w.itemText(i) for i in range(w.count())] != list(choices):
                with QSignalBlocker(w):
                    w.clear()
                    w.addItems(list(choices))
                    w.setCurrentText(field.get())

    def append_result(self, payload):
        i = self.table.rowCount()
        self.table.insertRow(i)
        for col, key in enumerate(
            (
                "step",
                "axis",
                "speed",
                "command",
                "actual",
                "error",
                "duration",
                "status",
            )
        ):
            v = payload[key]
            self.table.setItem(
                i, col, QTableWidgetItem(f"{v:.2f}" if isinstance(v, float) else str(v))
            )
