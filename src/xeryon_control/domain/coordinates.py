"""Encoder scale and per-axis user origins for one connection."""


def to_counts(value_um: float, counts_per_mm: float) -> int:
    return round((value_um / 1000.0) * counts_per_mm)


def from_counts(count: float, counts_per_mm: float) -> float:
    return (float(count) / counts_per_mm) * 1000.0


def to_user(encoder_um: float, zero_um: float) -> float:
    return encoder_um - zero_um


def to_encoder(user_um: float, zero_um: float) -> float:
    return zero_um + user_um


class AxisOrigins:
    """Keep each axis's zero across selection changes within the same session."""

    def __init__(self):
        self._zeros: dict[str, float] = {}
        self.active_um: float | None = None

    def capture(self, axis: str, encoder_um: float) -> None:
        self._zeros[axis] = self.active_um = encoder_um

    def select(self, axis: str, travel_confirmed: bool) -> None:
        self.active_um = self._zeros.get(axis) if travel_confirmed else None

    def forget(self, axis: str) -> None:
        self._zeros.pop(axis, None)
        self.active_um = None

    def clear(self) -> None:
        self._zeros.clear()
        self.active_um = None
