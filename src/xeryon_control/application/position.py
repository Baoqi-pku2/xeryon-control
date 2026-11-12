"""Encoder-driven position view. This widget never sends hardware commands."""
from dataclasses import dataclass
from math import ceil, floor, isfinite
import time
from xeryon_control.application.fields import Field
from xeryon_control.domain.coordinates import to_encoder, to_user
from xeryon_control.domain.travel import (
    DEFAULT_END_MARGIN_UM,
    DEFAULT_WORK_MAX_UM,
    DEFAULT_WORK_MIN_UM,
    DEFAULT_WORK_VIEW_MAX_UM,
    GEOMETRY_EPSILON_UM,
    STROKE_UM,
    TravelRange,
)


FEEDBACK_MAX_AGE = 2.0
FONT_UI = "Microsoft YaHei UI"
FONT_NUM = "Segoe UI"


@dataclass(frozen=True)
class TravelReadings:
    user_coordinates: bool
    overview_um: tuple[float, float]
    margin_um: float | None
    endpoints_um: tuple[float, float] | None
    distances_um: tuple[float, float] | None


class PositionModel:
    def __init__(self, app):
        self.app = app
        self.position = None
        self.received_at = None
        self.feedback_axis = None
        self.initialized_axis = None
        self.encoder_view_center = 0.0
        self.view_min, self.view_max = -STROKE_UM / 2, STROKE_UM / 2
        # Both tabs edit the same intended target; drawing never commands motion.
        self.target = app.target_position_var
        self.restoring_axis = False
        self.user_target = Field(value=self.target.get())
        self._syncing_target = False
        self.zero_text = Field()
        self.speed = Field(value="5")
        self.span = Field(value="1000")
        self.full_view = True
        self.center_coordinate = Field(value="0.00")
        self.end_margin = Field(value=f"{DEFAULT_END_MARGIN_UM:.0f}")
        self.center_checked = Field(value=False)
        self.travel_readings = Field(None)
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
        self.center_coordinate.subscribe(lambda *args: self.invalidate_mapping())
        self.end_margin.subscribe(lambda *args: self.invalidate_mapping())
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
        raw_um = float(raw_um)
        axis = self.app.active_axis_var.get()
        travel = TravelRange.from_retracted_endpoint(raw_um, axis)
        self.center_coordinate.set(f"{travel.center_um:.4f}")
        self.end_margin.set(f"{DEFAULT_END_MARGIN_UM:.0f}")
        self.center_checked.set(True)
        self.app.travel_range = travel
        self.app.travel_ranges[axis] = travel
        if not self.app.limit_enabled_var.get():
            safe_lo, safe_hi = travel.command_bounds
            self.app.min_limit_var.set(f"{safe_lo:.2f}")
            self.app.max_limit_var.set(f"{safe_hi:.2f}")
        self.app.origins.capture(axis, raw_um)
        self.position = raw_um
        self.position_count = self.app._um_to_inc(raw_um)
        self.received_at = sample_time
        self.feedback_axis = axis
        self.encoder_view_center = raw_um
        self.target.set(repr(raw_um))
        self.show_full_travel()

    def forget_axis_zero(self):
        self.app.origins.forget(self.app.active_axis_var.get())
        self.clear_user_zero()

    def clear_user_zero(self):
        self.encoder_view_center = 0.0
        self.full_view = True
        self._raw_target_changed()

    def use_current_position_as_center(self):
        if not self.is_fresh():
            self.app._show_error("请等待有效的编码器位置反馈。")
            return
        self.center_coordinate.set(f"{self.position:.4f}")
        self.center_checked.set(True)
        self.redraw()

    def _mapping_check_changed(self):
        if not self.center_checked.get():
            self.app.travel_range.invalidate()
        self.app.travel_ranges.pop(self.app.active_axis_var.get(), None)
        self.redraw()

    def invalidate_mapping(self):
        if self.restoring_axis:
            return
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
            return True
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
            message = ("已开启默认限位：目标 0–13000 µm，显示 0–15000 µm。"
                       if self.app.default_limit_enabled else "已关闭默认限位：恢复完整行程显示和目标范围。")
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
                return to_encoder(DEFAULT_WORK_MIN_UM, self.user_zero_um), to_encoder(DEFAULT_WORK_VIEW_MAX_UM, self.user_zero_um)
            return self.app.travel_range.mechanical_bounds
        if self.user_zero_set:
            # A nominal display span only; never used to authorize travel.
            return self.user_zero_um, to_encoder(STROKE_UM, self.user_zero_um)
        return self.encoder_view_center - STROKE_UM / 2, self.encoder_view_center + STROKE_UM / 2

    def target_bounds(self):
        if self.app.travel_range.confirmed:
            um_per_count = self.app._inc_to_um(1)
            low_inc, high_inc = self.app.travel_range.count_bounds(um_per_count)
            if self.default_limit_active():
                low_inc = max(low_inc, ceil((to_encoder(DEFAULT_WORK_MIN_UM, self.user_zero_um)) / um_per_count))
                high_inc = min(high_inc, floor((to_encoder(DEFAULT_WORK_MAX_UM, self.user_zero_um)) / um_per_count))
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
        travel = self.app.travel_range
        self.restoring_axis = True
        try:
            self.center_coordinate.set(f"{travel.center_um:.4f}")
            self.end_margin.set(f"{travel.margin_um:g}")
            self.center_checked.set(travel.confirmed)
        finally:
            self.restoring_axis = False
        self.app.origins.select(axis, travel.confirmed)
        self.encoder_view_center = self.user_zero_um
        self.full_view = True
        self._raw_target_changed()
        self.redraw()

    def invalidate(self):
        self.app.origins.clear()
        self.app.travel_range.invalidate()
        self.app.travel_ranges.clear()
        self.center_checked.set(False)
        self.position = self.received_at = self.feedback_axis = None
        self.initialized_axis = None
        self.clear_user_zero()
        self.redraw()

    def accept_status(self, status, *, initialize_target=True):
        if status is None:
            self.app.origins.clear()
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
        self.position_count = status.get("position_inc")
        self.received_at = status["sample_time"]
        self.feedback_axis = status["axis"]
        if self.initialized_axis != self.feedback_axis and self.is_fresh():
            self.initialized_axis = self.feedback_axis
            if initialize_target:
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
                lo, hi = self.target_bounds()
                self.target.set(f"{min(hi, max(lo, value)):.2f}")
            except ValueError:
                pass
        return "break"

    def preview_fraction(self, fraction):
        if not self.editing_locked():
            fraction = min(1.0, max(0.0, fraction))
            value = self.view_min + fraction * (self.view_max - self.view_min)
            lo, hi = self.target_bounds()
            # In an uncalibrated zoom view, negative user coordinates remain
            # previewable; only a confirmed mechanical range constrains them.
            value = min(hi, max(lo, value)) if self.app.travel_range.confirmed else value
            self.target.set(f"{value:.2f}")

    def start(self):
        self.app.start_position_panel_move()
        self.redraw()

    def redraw(self):
        fresh = self.is_fresh()
        locked = self.editing_locked()
        travel = self.app.travel_range
        linked = self.app.linked_mode_var.get()
        default_ready = not self.app.default_limit_enabled or self.default_limit_active()
        pending = self.app.reference_review is not None
        if pending or self.app.reconnect_reason:
            self.calibration_badge_text.set("需重新标定" if self.app.reconnect_reason else "零点待复核")
        elif travel.confirmed and self.user_zero_set:
            self.calibration_badge_text.set("默认限位 0–13 mm" if self.default_limit_active() else "已置零（0–60 mm）")
        elif travel.confirmed:
            self.calibration_badge_text.set("行程已确认")
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
        elif not default_ready:
            self.readiness_text.set("请先在收回端点置零")
        elif not travel.confirmed:
            self.readiness_text.set("请将推杆收回至端点后置零")
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
        target_lo, target_hi = self.target_bounds()
        if self.user_zero_set:
            self.zero_text.set("0 µm = 收回端点" if travel.confirmed else "已置零，行程待确认")
        else:
            self.zero_text.set("请在收回端点置零")
        if fresh:
            self.feedback_text.set(f"{time.monotonic() - self.received_at:.1f} s 前更新")
        elif self.position is not None:
            self.feedback_text.set("反馈过期，位置待确认")
        else:
            self.feedback_text.set("等待编码器反馈")
        if self.full_view:
            self.view_min, self.view_max = self.display_bounds()
        slider_lo, slider_hi = target_lo, target_hi
        self.range_text.set(("可设目标" if travel.confirmed else "目标预览") + f"：{(to_user(slider_lo, self.user_zero_um))/1000:+.2f} 至 {(to_user(slider_hi, self.user_zero_um))/1000:+.2f} mm")
        zero = self.user_zero_um if self.user_zero_set else 0.0
        endpoints = distances = None
        if travel.confirmed and travel.axis == self.app.active_axis_var.get():
            lo, hi = travel.mechanical_bounds
            endpoints = (lo - zero, hi - zero)
            if fresh:
                distances = (max(0.0, self.position - lo), max(0.0, hi - self.position))
        readings = TravelReadings(
            self.user_zero_set,
            tuple(bound - zero for bound in self.display_bounds()),
            travel.margin_um if endpoints is not None else None,
            endpoints,
            distances,
        )
        if readings != self.travel_readings.get():
            self.travel_readings.set(readings)
        try:
            target = float(self.target.get())
            if not isfinite(target):
                raise ValueError()
        except ValueError:
            target = None
        if target is None and not self.app.stop_pending and not self.app.stop_unconfirmed:
            self.readiness_text.set("请输入有效目标坐标")
        elif travel.confirmed and not self.app.stop_pending and not self.app.stop_unconfirmed:
            try:
                travel.validate(target, self.app._um_to_inc(target), self.app._inc_to_um(1), self.app.active_axis_var.get())
                self.app._validate_default_limit(target)
            except ValueError:
                self.readiness_text.set("目标超出范围")
        self.delta_text.set(f"待移动  {target - self.position:+,.2f} µm" if fresh and target is not None else "待移动  —")

        self.on_change()
