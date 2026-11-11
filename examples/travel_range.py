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


def next_workflow_stage(positions, start, switch, end, window, retreat):
    """Plan one monotonic stage in user coordinates for one or both axes."""
    direction = 1 if end > start else -1
    boundary = direction * (switch - start)
    length = direction * (end - start)
    if not 0 < boundary < length or not 0 < window < min(boundary, length - boundary) / 2:
        raise ValueError("工作起点、切换点、终点须依次排列，两段长度均须大于两倍到位窗口。")
    progress = [direction * (position - start) for position in positions]
    low, high = min(progress), max(progress)
    if low < -window or high > length + window:
        detail = " / ".join(f"{position:.2f}" for position in positions)
        raise ValueError(f"当前位置 {detail} µm 超出分段范围 {start:g} 至 {end:g} µm，请核对位置。")
    if retreat:
        if high <= window:
            return None
        # Retreat slowly past the switch. Both axes must clear its window
        # before accelerating. A pair straddling the transition never reverses.
        if high >= boundary - window:
            clearance = boundary - 3 * window
            if clearance > window and low > clearance + window:
                return start + direction * clearance, "慢速"
            return start, "慢速"
        return start, "快速"
    if low >= length - window:
        return None
    if high < boundary - window:
        return switch, "快速"
    return end, "慢速"
