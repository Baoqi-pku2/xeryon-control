"""Encoder-driven position view. This widget never sends hardware commands."""
from math import isfinite
import time
from xeryon_control.application.fields import Field
from xeryon_control.domain.coordinates import to_encoder, to_user


FEEDBACK_MAX_AGE = 2.0
CONSOLE_SPAN_UM = 13_000.0
CONSOLE_SLIDER_MIN_UM = 50.0
CONSOLE_TARGET_HINT = "控制台目标范围为 0–13000 µm"
FONT_UI = "Microsoft YaHei UI"
FONT_NUM = "Segoe UI"


def valid_console_coordinate(text):
    try:
        return 0 <= float(text) <= CONSOLE_SPAN_UM
    except ValueError:
        return False

class PositionModel:
    def __init__(self, app):
        self.app = app
        self.position = None
        self.received_at = None
        self.feedback_axis = None
        self.initialized_axis = None
        self.encoder_view_center = 0.0
        self.view_min, self.view_max = 0.0, CONSOLE_SPAN_UM
        # Both tabs edit the same intended target; drawing never commands motion.
        self.target = app.target_position_var
        self.user_target = Field(value=self.target.get())
        self._syncing_target = False
        self.zero_text = Field()
        self.offset_coordinate = Field(value="0")
        self.speed = Field(value="5")
        self.span = Field(value="1000")
        self.full_view = True
        self.actual_text = Field(value="— µm")
        self.feedback_text = Field(value="等待连接")
        self.encoder_caption = Field()
        self.position_count = None
        self.delta_text = Field(value="—")
        self.readiness_text = Field()
        self.calibration_badge_text = Field(value="零点未标定")
        self.actual_caption = Field()
        self.target_caption = Field()
        self.range_text = Field()
        self.on_change = lambda: None
        self.target.subscribe(self._raw_target_changed)
        self.user_target.subscribe(self._user_target_changed)
        app.active_axis_var.subscribe(self._axis_changed)
        self.redraw()

    @property
    def user_zero_set(self):
        return self.app.origins.active_um is not None

    @property
    def user_zero_um(self):
        origin = self.app.origins.active_um
        return 0.0 if origin is None else origin

    def _raw_target_changed(self, *args):
        if self._syncing_target:
            return
        self._syncing_target = True
        try:
            try:
                value = float(self.target.get())
                text = f"{to_user(value, self.user_zero_um):.2f}" if isfinite(value) else self.target.get()
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
                text = repr(to_encoder(value, self.user_zero_um)) if isfinite(value) else self.user_target.get()
            except ValueError:
                text = self.user_target.get()
            self.target.set(text)
        finally:
            self._syncing_target = False
        self.redraw()

    def apply_user_zero(self, raw_um, sample_time):
        self.apply_offset_zero(float(raw_um), sample_time, 0.0)

    def apply_offset_zero(self, raw_um, sample_time, coordinate):
        axis = self.app.active_axis_var.get()
        self.app.origins.capture(axis, raw_um - coordinate)
        self.offset_coordinate.set(str(coordinate))
        self._finish_zero(raw_um, sample_time)

    def _finish_zero(self, raw_um, sample_time):
        self.position = raw_um
        self.position_count = self.app._um_to_inc(raw_um)
        self.received_at = sample_time
        self.feedback_axis = self.app.active_axis_var.get()
        self.initialized_axis = self.feedback_axis
        self.app.zero_recovery.capture_zero(self.feedback_axis, self.position_count, sample_time)
        self.encoder_view_center = raw_um
        self.target.set(repr(raw_um))
        self.show_full_travel()

    def forget_axis_zero(self):
        self.app.origins.forget(self.app.active_axis_var.get())
        self.clear_user_zero()

    def clear_user_zero(self):
        self.offset_coordinate.set("0")
        self.encoder_view_center = 0.0
        self.full_view = True
        self._raw_target_changed()

    def show_full_travel(self):
        self.full_view = True
        self.view_min, self.view_max = self.display_bounds()
        self.redraw()

    def display_bounds(self):
        return self.user_zero_um, self.user_zero_um + CONSOLE_SPAN_UM

    def slider_bounds(self):
        return self.user_zero_um + CONSOLE_SLIDER_MIN_UM, self.user_zero_um + CONSOLE_SPAN_UM


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
        self.position = self.received_at = self.feedback_axis = None
        self.initialized_axis = None
        self.app.origins.select(axis)
        self.encoder_view_center = self.user_zero_um
        self.offset_coordinate.set("0")
        self.full_view = True
        self._raw_target_changed()
        self.redraw()

    def invalidate(self):
        self.app.origins.clear()
        self.position = self.received_at = self.feedback_axis = None
        self.initialized_axis = None
        self.clear_user_zero()
        self.redraw()

    def accept_status(self, status, *, initialize_target=True):
        if status is None:
            self.app.origins.clear()
            # Preserve the last reading and intended target, marked stale.
            self.received_at = None
            self.clear_user_zero()
            self.redraw()
            return
        if status.get("axis") != self.app.active_axis_var.get():
            return
        self.position = float(status["position_um"])
        self.position_count = status.get("position_inc")
        self.received_at = status["sample_time"]
        self.feedback_axis = status["axis"]
        if self.initialized_axis != self.feedback_axis and self.is_fresh():
            self.initialized_axis = self.feedback_axis
            if self.user_zero_set:
                self.encoder_view_center = self.position
            if initialize_target:
                self.target.set(f"{self.position:.2f}")
            self.show_full_travel()
        self.redraw()

    def center_view(self):
        try:
            span = float(self.span.get())
            if not isfinite(span) or span < 10:
                raise ValueError("局部显示跨度至少为 10 µm。")
            center = self.position if self.position is not None else self.encoder_view_center
            lo, hi = self.display_bounds()
            if span > CONSOLE_SPAN_UM:
                span = CONSOLE_SPAN_UM
                self.span.set(f"{span:g}")
            self.view_min = max(lo, min(center - span / 2, hi - span))
            self.view_max = self.view_min + span
            self.full_view = False
            self.redraw()
        except ValueError as exc:
            self.app._show_error(str(exc))

    def editing_locked(self):
        return (self.app.command_busy or self.app.start_confirmation is not None or self.app.active_target_inc is not None
                or self.app.shortcut_active is not None or bool(self.app.shortcut_pending)
                or self.app.stop_pending or self.app.stop_unconfirmed)

    def slide_target(self, value):
        if not self.editing_locked():
            self.target.set(f"{float(value):.2f}")

    def nudge(self, direction):
        if not self.editing_locked():
            try:
                value = float(self.target.get()) + direction * self.app._inc_to_um(1)
                lo, hi = self.display_bounds()
                self.target.set(f"{min(hi, max(lo, value)):.2f}")
            except ValueError:
                pass
        return "break"

    def preview_fraction(self, fraction):
        if not self.editing_locked():
            fraction = min(1.0, max(0.0, fraction))
            value = self.view_min + fraction * (self.view_max - self.view_min)
            self.target.set(f"{value:.2f}")

    def start(self):
        self.app.start_position_panel_move()
        self.redraw()

    def redraw(self):
        fresh = self.is_fresh()
        locked = self.editing_locked()
        linked = self.app.linked_mode_var.get()
        pending = self.app.reference_review is not None
        if pending or self.app.reconnect_reason:
            self.calibration_badge_text.set("需重新标定" if self.app.reconnect_reason else "零点待复核")
        elif self.user_zero_set:
            self.calibration_badge_text.set("已置零")
        else:
            self.calibration_badge_text.set("零点未标定")

        if self.app.stop_unconfirmed:
            self.readiness_text.set("停止未确认。请断开驱动电源后重新连接")
        elif self.app.stop_pending:
            self.readiness_text.set("正在确认驱动状态和位置")
        elif self.app.reconnect_reason or pending:
            self.readiness_text.set(self.app.reconnect_reason or "通信中断，请复核原零点")
        elif locked:
            self.readiness_text.set("命令执行中；STOP 可停止")
        elif self.app.node is None:
            self.readiness_text.set("请连接设备")
        elif not fresh:
            self.readiness_text.set("等待有效位置反馈")
        elif linked:
            self.readiness_text.set("请在高级设置中关闭双轴联动")
        else:
            self.readiness_text.set("已就绪")
        relative = to_user(self.position, self.user_zero_um) if self.user_zero_set and self.position is not None else None
        self.actual_text.set(f"{relative:,.2f}" if relative is not None else "—")
        self.app.user_position_var.set(f"{relative:.2f}" if fresh and relative is not None else "—")
        caption = "相对用户零点" if self.user_zero_set else "尚未置零"
        if self.position is not None and not fresh:
            caption += "（反馈已过期）"
        self.actual_caption.set(caption)
        self.target_caption.set("相对目标（µm）" if self.user_zero_set else "编码器目标（µm）")
        raw_label = "编码器绝对位置" if fresh else "上次编码器位置"
        counts = f"（{self.position_count} 计数）" if self.position_count is not None else ""
        self.encoder_caption.set(f"{raw_label} {self.position:.2f} µm{counts}" if self.position is not None else "")
        if self.user_zero_set:
            self.zero_text.set(f"用户零点对应编码器 {self.user_zero_um:,.2f} µm")
        else:
            self.zero_text.set("请设置用户零点")
        if fresh:
            self.feedback_text.set(f"{time.monotonic() - self.received_at:.1f} s 前更新")
        elif self.position is not None:
            self.feedback_text.set("反馈过期，位置待确认")
        else:
            self.feedback_text.set("等待编码器反馈")
        if self.full_view:
            self.view_min, self.view_max = self.display_bounds()
        self.range_text.set(f"控制台范围  0–{CONSOLE_SPAN_UM / 1000:g} mm")
        try:
            target = float(self.target.get())
            if not isfinite(target):
                raise ValueError()
        except ValueError:
            target = None
        if target is None and not self.app.stop_pending and not self.app.stop_unconfirmed:
            self.readiness_text.set("请输入有效目标坐标")
        elif target is not None and not valid_console_coordinate(self.user_target.get()) and not locked:
            self.readiness_text.set(CONSOLE_TARGET_HINT)
        self.delta_text.set(f"待移动  {target - self.position:+,.2f} µm" if fresh and target is not None else "待移动  —")

        self.on_change()
