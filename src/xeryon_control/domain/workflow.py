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
