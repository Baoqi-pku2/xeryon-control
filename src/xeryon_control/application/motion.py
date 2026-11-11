"""Snapshots used to review and resume a single motion request."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from canopen import BaseNode402


@dataclass(frozen=True)
class PanelMotion:
    label: str
    target: float
    profile: dict
    limits: dict
    node: BaseNode402
    axis: str
    zero: float
    zero_set: bool
    displayed_inc: int
    bounds: tuple
    generation: int
    step: Optional[dict]


class StartupDisplacement(RuntimeError):
    def __init__(self, message, previous_inc, stopped):
        super().__init__(message)
        self.previous_inc = previous_inc
        self.stopped = stopped


@dataclass(frozen=True)
class StartConfirmation:
    motion: PanelMotion
    previous_inc: int
    stopped_inc: int
