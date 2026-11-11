"""XLA-5-95 geometry and session-specific encoder-to-travel mapping.

Manufacturer XLA-5 v2.3, mechanical-properties table: 95 mm rod, 60 mm
nominal travel (+/-0.3 mm). A rod length is not a travel length. The
incremental encoder needs an operator-established mechanical reference.
"""
from dataclasses import dataclass
from math import ceil, floor, isfinite

MODEL = "XLA-5-95-1MU-INTG"
ROD_LENGTH_UM = 95_000.0
STROKE_UM = 60_000.0
DEFAULT_END_MARGIN_UM = 50.0
DEFAULT_WORK_MIN_UM = 0.0
DEFAULT_WORK_MAX_UM = 13_000.0
DEFAULT_WORK_VIEW_MAX_UM = 15_000.0
# Arithmetic cancellation in (zero + 30000) - 30000 can move a boundary
# by picometres. This tolerance is ONLY for float geometry; count bounds stay exact.
GEOMETRY_EPSILON_UM = 1e-8


@dataclass
class TravelRange:
    center_um: float = 0.0
    margin_um: float = DEFAULT_END_MARGIN_UM
    axis: str | None = None

    @property
    def confirmed(self):
        return self.axis is not None

    @classmethod
    def from_retracted_endpoint(cls, encoder_um: float, axis: str):
        travel = cls()
        travel.confirm(encoder_um + STROKE_UM / 2, DEFAULT_END_MARGIN_UM, axis)
        return travel

    @property
    def mechanical_bounds(self):
        return self.center_um - STROKE_UM / 2, self.center_um + STROKE_UM / 2

    @property
    def command_bounds(self):
        lo, hi = self.mechanical_bounds
        return lo + self.margin_um, hi - self.margin_um

    def confirm(self, center_um, margin_um, axis):
        if not isfinite(center_um):
            raise ValueError("机械中位坐标必须是有限数值。")
        if not isfinite(margin_um) or not 50 <= margin_um < STROKE_UM / 2:
            raise ValueError("每端预留需在 50–29999 µm 之间；默认 50 µm。")
        if not axis:
            raise ValueError("缺少活动轴。")
        self.center_um, self.margin_um, self.axis = center_um, margin_um, axis

    def invalidate(self):
        self.axis = None

    def count_bounds(self, um_per_count):
        lo, hi = self.command_bounds
        return ceil(lo / um_per_count), floor(hi / um_per_count)

    def validate(self, target_um, target_inc, um_per_count, axis):
        if not self.confirmed or axis != self.axis:
            raise ValueError("请先在行程设置中确认本次连接的机械中位坐标。")
        lo, hi = self.command_bounds
        low_inc, high_inc = self.count_bounds(um_per_count)
        if (not isfinite(target_um) or not lo - GEOMETRY_EPSILON_UM <= target_um <= hi + GEOMETRY_EPSILON_UM
                or not low_inc <= target_inc <= high_inc):
            raise ValueError(
                f"实际目标 {target_um:.2f} µm（{target_inc} 计数，轴 {axis}）"
                f"超出已确认的行程范围 {lo:.2f} 至 {hi:.2f} µm（已扣除端部预留）。"
            )
