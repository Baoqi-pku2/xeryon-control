"""Capture an endpoint across the disabled, enabled and settled samples."""
import time


def sample_stationary_encoder(*, read_position, is_disabled, enable_and_sample, is_enabled,
                     disable_and_settle, check_cancelled, stop_event):
    check_cancelled()
    if not is_disabled():
        raise ValueError("请先按 STOP，确认驱动已关闭。")
    first = read_position()
    time.sleep(0.15)
    baseline = read_position()
    if abs(baseline - first) > 1:
        raise ValueError("推杆仍在移动，采样已取消。")
    try:
        enabled = enable_and_sample(baseline)
        settle_until = time.monotonic() + 0.15
        while True:
            check_cancelled()
            last = read_position()
            if abs(last - enabled) > 1 or not is_enabled():
                raise ValueError("驱动开启时位置或状态有变。采样已取消。")
            remaining = settle_until - time.monotonic()
            if remaining <= 0:
                break
            stop_event.wait(min(0.025, remaining))
    finally:
        disabled, position = disable_and_settle()
    check_cancelled()
    if not disabled or abs(position - enabled) > 1 or abs(position - last) > 1:
        raise ValueError("驱动关闭前后读数不一致。采样已取消。")
    return baseline, enabled, position
