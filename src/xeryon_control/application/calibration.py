"""Capture an endpoint across the disabled, enabled and settled samples."""
import time


def capture_endpoint(*, read_position, is_disabled, enable_and_sample, is_enabled,
                     disable_and_settle, check_cancelled, stop_event):
    check_cancelled()
    if not is_disabled():
        raise ValueError("请先 STOP，确认驱动器失能后再置零。")
    first = read_position()
    time.sleep(0.15)
    baseline = read_position()
    if abs(baseline - first) > 1:
        raise ValueError("位置仍在变化，未设置零点。")
    try:
        enabled = enable_and_sample(baseline)
        settle_until = time.monotonic() + 0.15
        while True:
            check_cancelled()
            last = read_position()
            if abs(last - enabled) > 1 or not is_enabled():
                raise ValueError("使能采样期间位置或驱动状态发生变化，未设置零点。")
            remaining = settle_until - time.monotonic()
            if remaining <= 0:
                break
            stop_event.wait(min(0.025, remaining))
    finally:
        disabled, position = disable_and_settle()
    check_cancelled()
    if not disabled or abs(position - enabled) > 1 or abs(position - last) > 1:
        raise ValueError("失能后的反馈与使能采样不一致，未设置零点。")
    return baseline, enabled, position
