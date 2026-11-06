"""Bounded, acknowledged shutdown. No state-machine path may re-enable motion."""
import time


def verify_disabled(node, timeout=1.5, settle=0.15):
    """Request disable voltage over SDO; verify live state AND stationary encoder.

    Caller sends independent RPDO stop frames first, including to other axes.
    A failed acknowledgement/read is an unknown stop, never a successful stop.
    """
    sdo = node.sdo
    old_timeout, old_retries = sdo.RESPONSE_TIMEOUT, sdo.MAX_RETRIES
    deadline = time.monotonic() + timeout
    try:
        sdo.RESPONSE_TIMEOUT = min(old_timeout, 0.3)
        sdo.MAX_RETRIES = 1
        sdo[0x6040].raw = 0x00
        last_position = None
        stable_since = None
        while time.monotonic() < deadline:
            status = int(sdo[0x6041].raw)
            disabled = (status & 0x4F) in (0x40, 0x08) or (status & 0x6F) in (0x21, 0x23)
            position = int(sdo[0x6064].raw)
            now = time.monotonic()
            if disabled and last_position is not None and abs(position - last_position) <= 1:
                if stable_since is not None and now - stable_since >= settle:
                    return {"statusword": status, "position_inc": position}
            else:
                stable_since = now if disabled else None
                last_position = position
            time.sleep(0.05)
        raise RuntimeError("停止未确认：状态或编码器仍未稳定")
    finally:
        sdo.RESPONSE_TIMEOUT, sdo.MAX_RETRIES = old_timeout, old_retries
