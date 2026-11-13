"""Main console, using the same controller fields as the advanced pages."""

from math import isfinite
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QSlider
from xeryon_control.domain.shortcut_presets import SHORTCUT_PRESETS
from xeryon_control.domain.travel import MODEL
from xeryon_control.ui.icons import icon
from xeryon_control.ui.travel_readout import PositionSummary
from xeryon_control.ui.plot import PositionPlot
from xeryon_control.ui.widgets import (
    label,
    value_label,
    button,
    edit,
    combo,
    card,
    row,
    divider,
    glyph,
    PresetButton,
    segments,
)


def signed_readout(text):
    return (
        "+" + text if text[:1].isdigit() and float(text.replace(",", "")) > 0 else text
    )


class PositionPanel(QWidget):
    def __init__(self, session):
        super().__init__()
        self.setObjectName("page")
        self.session = s = session
        self.model = m = s.position_panel
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(9)

        connect, bar = card()
        self.port = combo(s.channel_var, s.serial_ports, True)
        self.connect_button = button("连接设备", self.toggle_connection, "connect")
        self.zero_button = button("收回端点置零", self.zero)
        self.port.setMinimumWidth(90)
        self.refresh_button = button("刷新", s.refresh_device, "quiet")
        self.refresh_button.setIcon(icon("refresh"))
        self.zero_button.setIcon(icon("position"))
        self.zero_state = value_label(m.calibration_badge_text, "badge")
        self.sampling_hint = label("", "muted")
        self.connection_row = row(
            glyph("port"),
            label("串口"),
            self.port,
            self.refresh_button,
            self.connect_button,
            divider(True),
            label("节点", "muted"),
            value_label(s.node_id_var),
            divider(True),
            self.zero_button,
            self.sampling_hint,
            divider(True),
            self.zero_state,
            None,
        )
        bar.addLayout(self.connection_row)
        outer.addWidget(connect)

        self.body = body = QGridLayout()
        body.setHorizontalSpacing(9)
        body.setColumnStretch(0, 7)
        body.setColumnStretch(1, 5)
        outer.addLayout(body, 1)
        self.visual, plot_box = card()
        self.motion, form = card()
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(10)
        body.addWidget(self.visual, 0, 0)
        body.addWidget(self.motion, 0, 1)
        self.full = button("整体视图", m.show_full_travel, "segment")
        self.zoom = button("放大当前位置", m.center_view, "segment")
        for b in (self.full, self.zoom):
            b.setCheckable(True)
        plot_box.addLayout(
            row(
                glyph("position"),
                label("位置示意", "title"),
                divider(True),
                label(MODEL, "muted"),
                None,
                button("行程检查", s.view.show_motion_setup, "quiet"),
                segments(self.full, self.zoom),
            )
        )
        self.plot = PositionPlot(m)
        plot_box.addWidget(self.plot, 1)
        self.summary = PositionSummary(m)
        plot_box.addWidget(self.summary)

        form.addLayout(
            row(glyph("position"), label("当前位置", "title"), None, value_label(m.actual_caption, "muted"))
        )
        self.actual = value_label(m.actual_text, "readout", display=signed_readout)
        form.addLayout(row(self.actual, label("µm", "unit"), None))
        form.addWidget(value_label(m.zero_text, "muted", True))
        form.addStretch(1)
        form.addWidget(divider())
        self.entry = edit(m.user_target)
        self.entry.setObjectName("targetInput")
        self.entry.setMaximumWidth(200)
        form.addLayout(
            row(
                value_label(
                    m.target_caption,
                    None,
                    False,
                    lambda text: text.removesuffix("（µm）"),
                ),
                self.entry,
                label("µm"),
                None,
                value_label(m.delta_text, "delta"),
            )
        )
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        self.slider.valueChanged.connect(self.slider_changed)
        form.addWidget(self.slider)
        form.addLayout(
            row(value_label(m.range_text, "muted"), None, label("拖动仅预览", "muted"))
        )
        form.addStretch(1)
        form.addWidget(divider())
        self.speed = edit(m.speed)
        self.speed.setObjectName("speedInput")
        self.speed.setMaximumWidth(155)
        form.addLayout(
            row(label("运动速度"), self.speed, label("µm/s"), None, label("范围 2–500 µm/s", "muted"))
        )
        self.speeds = {}
        for name, speed in (("慢速", 5), ("中速", 20), ("快速", 50), ("高速", 300)):
            b = button(
                f"{name}  {speed}", lambda n=speed: m.speed.set_from_user(str(n)), "segment"
            )
            b.setCheckable(True)
            self.speeds[speed] = b
        form.addWidget(segments(*self.speeds.values()))
        form.addStretch(1)
        form.addWidget(divider())
        status = row(
            glyph("sequence", 17),
            label("编码器分辨率", "muted"),
            value_label(s.encoder_var),
            divider(True),
            value_label(m.readiness_text, None, True),
        )
        status.setStretch(4, 1)
        form.addLayout(status)
        self.start = button("开始运动", m.start, "primary")
        self.start.setIcon(icon("play", "#ffffff"))
        self.stop = button("停止  STOP", s.stop_actuator_test, "localStop")
        self.stop.setIcon(icon("stop", "#b82539"))
        for control in (self.start, self.stop):
            control.setMinimumHeight(39)
        form.addLayout(row(self.start, self.stop))
        form.addWidget(divider())
        form.addLayout(
            row(
                glyph("pulse", 17),
                label("执行", "muted"),
                value_label(s.target_status_var, None, True),
                None,
                divider(True),
                label("驱动", "muted"),
                value_label(s.state_var),
            )
        )

        shortcuts, sequence = card()
        sequence.setSpacing(7)
        sequence.addLayout(row(glyph("sequence"), label("快捷流程", "title"), None))
        self.dwell = edit(s.shortcut_dwell_var)
        self.dwell.setMaximumWidth(52)
        self.clear = button("清空选择", s.clear_shortcut_queue, "quiet")
        self.clear.setIcon(icon("clear"))
        self.presets = {}
        self.preset_grid = QGridLayout()
        self.preset_grid.setSpacing(7)
        sequence.addLayout(self.preset_grid)
        for number, name, target, speed in SHORTCUT_PRESETS:
            self.presets[number] = PresetButton(
                number, name, f"{target:g}", speed, s.toggle_shortcut
            )
        self.workflow_start = None
        self.columns = 0
        sequence.addWidget(divider())
        footer = row(
            value_label(s.shortcut_status_var, "muted", True),
            divider(True),
            glyph("clock", 16),
            label("每步到位停留"),
            self.dwell,
            label("秒后自动继续"),
            None,
            self.clear,
        )
        footer.setStretch(0, 1)
        footer.setStretch(6, 2)
        sequence.addLayout(footer)
        outer.addWidget(shortcuts)
        self._ports = None
        self.reflow(7)
        m.on_change = self.refresh
        self.refresh()

    def reflow(self, columns):
        if self.columns == columns:
            return
        self.columns = columns
        narrow = columns < 7
        self.sampling_hint.setText("刷新或置零前请松手\n驱动可能短暂开启" if narrow
                                   else "刷新或置零前请松手，驱动可能短暂开启")
        self.body.addWidget(self.motion, 1 if narrow else 0, 0 if narrow else 1)
        self.body.setColumnStretch(1, 0 if narrow else 5)
        for i, b in enumerate(self.presets.values()):
            self.preset_grid.addWidget(b, i // columns, i % columns)

    def toggle_connection(self):
        if self.session.network is not None:
            self.session.disconnect_device()
        else:
            self.model.connect()

    def zero(self):
        if self.session.reference_review is not None:
            self.session.review_reference()
        else:
            self.session.set_current_user_zero()

    def slider_changed(self, n):
        lo, hi = self.model.target_bounds()
        self.model.slide_target(lo + (hi - lo) * n / 10000)

    def refresh(self):
        s, m = self.session, self.model
        locked = m.editing_locked()
        fresh = m.is_fresh()
        for w in (
            self.entry,
            self.speed,
            self.slider,
            self.dwell,
            *self.speeds.values(),
            *self.presets.values(),
        ):
            w.setEnabled(not locked)
        self.start.setEnabled(
            not (locked or s.reference_review is not None or s.reconnect_reason)
        )
        self.zero_button.setText(
            "复核零点" if s.reference_review is not None else "收回端点置零"
        )
        self.zero_button.setEnabled(
            bool(
                not locked
                and fresh
                and s.node
                and not s.reconnect_reason
                and (not s.linked_mode_enabled or s.reference_review is not None)
            )
        )
        self.connect_button.setText("断开设备" if s.network is not None else "连接设备")
        self.connect_button.setEnabled(
            not s.command_busy and not s.stop_pending and not s.disconnect_in_progress
        )
        self.refresh_button.setText("刷新位置" if s.network is not None else "刷新串口")
        self.refresh_button.setEnabled(not locked and not (s.disconnect_in_progress or s.close_in_progress))
        self.port.setEnabled(s.network is None and not s.command_busy)
        ports = tuple(s.serial_ports)
        if ports != self._ports:
            self._ports = ports
            with QSignalBlocker(self.port):
                self.port.clear()
                self.port.addItems(ports)
                self.port.setCurrentText(s.channel_var.get())
        if self.actual.property("stale") != (not fresh):
            self.actual.setProperty("stale", not fresh)
            self.actual.style().unpolish(self.actual)
            self.actual.style().polish(self.actual)
        self.full.setChecked(m.full_view)
        self.zoom.setChecked(not m.full_view)
        for n, b in self.speeds.items():
            b.setChecked(str(n) == m.speed.get())
        for n, b in self.presets.items():
            active = bool(s.shortcut_active and s.shortcut_active["number"] == n)
            b.setChecked(n in s.shortcut_selected or active)
            if b.property("activeStep") != active:
                b.setProperty("activeStep", active)
                b.style().unpolish(b)
                b.style().polish(b)
        if self.workflow_start != s.workflow_start_var.get():
            self.workflow_start = s.workflow_start_var.get()
            self.presets[5].set_step(
                "撤到工作起点", self.workflow_start, SHORTCUT_PRESETS[4][3]
            )
        self.clear.setEnabled(bool(s.shortcut_selected and not locked))
        lo, hi = m.target_bounds()
        try:
            value = float(m.target.get())
        except ValueError:
            value = float("nan")
        if isfinite(value) and hi > lo:
            with QSignalBlocker(self.slider):
                self.slider.setValue(
                    round(10000 * (min(hi, max(lo, value)) - lo) / (hi - lo))
                )
        self.plot.update()
