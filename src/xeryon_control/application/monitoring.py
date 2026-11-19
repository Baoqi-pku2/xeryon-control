"""Passive encoder acquisition for console moves. Never reads or writes the bus."""

from dataclasses import dataclass
from threading import Lock
from time import monotonic, time


@dataclass(frozen=True, eq=False)
class MonitorSegment:
    clicked: float
    axis: str
    name: str
    scale: float
    zero: float
    calibrated: bool


class MotionCapture:
    def __init__(self, post):
        self.post = post
        self.lock = Lock()
        self.node = self.segment = None

    def begin(self, node, segment, position, stamp):
        with self.lock:
            self.node, self.segment = node, segment
            self.wall_offset = time() - monotonic()
            self.last_stamp = self.last_pdo = float("-inf")
            self._sample(position, stamp)

    def on_pdo(self, node, pdo):
        with self.lock:
            if self.node is not node:
                return
            try:
                position = pdo["Position Actual Value"].raw
            except KeyError:  # Custom mappings may provide only SDO feedback.
                return
            # python-can stamps the received frame in wall-clock seconds.
            # Anchor once per segment to the monotonic clock used by SDO reads.
            self.last_pdo = pdo.timestamp - self.wall_offset
            if self.last_pdo > monotonic() + 0.05:
                self.last_pdo = monotonic()
            self._sample(position, self.last_pdo)

    def on_sdo(self, node, position, stamp):
        with self.lock:
            if self.node is node and stamp - self.last_pdo > 0.25:
                self._sample(position, stamp)

    def _sample(self, count, stamp):
        if stamp <= self.last_stamp:
            return
        self.last_stamp = stamp
        seg = self.segment
        self.post("monitor_sample", (seg, stamp, count * seg.scale - seg.zero))

    def stopped_position(self, node, position):
        with self.lock:
            if self.node is node:
                self._sample(position, monotonic())

    def finish(self, node=None):
        with self.lock:
            if self.segment is not None and (node is None or self.node is node):
                self.post("monitor_end", self.segment)
                self.node = self.segment = None
