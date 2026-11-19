"""Event-based user-origin snapshots; recovery never commands a return move."""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime

from xeryon_control.domain.coordinates import from_counts


class ZeroRecovery:
    def __init__(self, session):
        self.session = session
        self._records = {}
        self._suspended_axes = set()
        self._lock = threading.RLock()

    def key(self, axis):
        s = self.session
        node = s.nodes.get(axis)
        if node is None:
            return None
        identity = s.node_identities.get(node.id)
        if (isinstance(identity, (list, tuple)) and len(identity) == 4
                and all(isinstance(v, int) for v in identity) and identity[-1]):
            device = ["identity", *identity]
        else:
            # Some controllers have no unique serial. Require the operator to
            # confirm the same physical actuator on the saved connection.
            address = getattr(s, "connection_address", (s.interface_var.get(), s.channel_var.get()))
            device = ["connection", *address, node.id]
        return json.dumps([device, float(s.inc_per_mm)], ensure_ascii=False, separators=(",", ":"))

    def record(self, axis=None):
        key = self.key(axis or self.session.active_axis_var.get())
        with self._lock:
            record = self._records.get(key)
            return dict(record) if record else None

    @staticmethod
    def coordinate(record):
        return from_counts(record["position_inc"], record["counts_per_mm"]) - record["zero_um"]

    def _capture(self, axis, count, *, saved_at, source):
        s = self.session
        if s.reference_review is not None or s.reconnect_reason:
            return
        key, zero = self.key(axis), s.origins.get(axis)
        if key is None or zero is None:
            # Reconnecting without restoring must not erase the saved origin.
            return
        record = dict(position_inc=int(count), zero_um=float(zero), counts_per_mm=s.inc_per_mm,
                      saved_at=saved_at, source=source, axis=axis)
        with self._lock:
            if axis in self._suspended_axes:
                return
            previous = self._records.get(key)
            if previous and previous["saved_at"] > saved_at:
                return
            self._records[key] = record
        # Disk access belongs to the UI persistence adapter, never the STOP lane.
        s._post("save_zero_recovery", None)

    def capture_zero(self, axis, count, sample_time):
        with self._lock:
            self._suspended_axes.discard(axis)
        self._capture(axis, count, saved_at=time.time() - max(0, time.monotonic() - sample_time),
                      source="置零后读数")

    def suspend_axis(self, axis):
        # Homing changes the encoder reference before the queued UI zero-clear
        # necessarily runs. Do not combine new counts with the previous origin.
        with self._lock:
            self._suspended_axes.add(axis)

    def capture_stopped(self, results):
        for axis, node in self.session.nodes.items():
            result = results.get(node.id)
            if result is not None:
                self._capture(axis, result["position_inc"], saved_at=time.time(), source="停止后读数")

    def capture_before_disconnect(self):
        s = self.session
        p = s.position_panel
        if p.position is None or p.received_at is None or p.feedback_axis != s.active_axis_var.get():
            return
        self._capture(p.feedback_axis, s._um_to_inc(p.position),
                      saved_at=time.time() - max(0, time.monotonic() - p.received_at),
                      source="断开前末次反馈")

    def serialize(self):
        with self._lock:
            return json.dumps({"version": 1, "records": self._records}, ensure_ascii=False, allow_nan=False)

    def load(self, text):
        data = json.loads(text)
        if data.get("version") != 1 or not isinstance(data.get("records"), dict):
            raise ValueError("不支持的零点记录格式")
        records = data["records"]
        for key, r in records.items():
            if (not isinstance(key, str) or not isinstance(r, dict)
                    or type(r.get("position_inc")) is not int
                    or not -2**31 <= r["position_inc"] < 2**31
                    or r.get("source") not in ("置零后读数", "停止后读数", "断开前末次反馈")
                    or not isinstance(r.get("axis"), str)):
                raise ValueError("零点记录损坏")
            for name in ("zero_um", "counts_per_mm", "saved_at"):
                value = r.get(name)
                if type(value) not in (float, int) or not math.isfinite(value):
                    raise ValueError("零点记录含无效数值")
            if r["counts_per_mm"] <= 0 or not 0 < r["saved_at"] < 253402214400:
                raise ValueError("零点记录含无效比例或时间")
            if (not math.isfinite(self.coordinate(r))
                    or abs(self.coordinate(r)) > from_counts(2**32 - 1, r["counts_per_mm"])):
                raise ValueError("零点记录超出范围")
        with self._lock:
            self._records = records

    def description(self):
        if not self.session.nodes:
            return "连接设备后可查看恢复记录"
        record = self.record()
        if not record:
            return "本设备暂无零点记录"
        stamp = datetime.fromtimestamp(record["saved_at"]).strftime("%m-%d %H:%M:%S")
        return f"{stamp}  用户位置 {self.coordinate(record):+.2f} µm（{record['source']}）"

    def _check_ready(self):
        s, p = self.session, self.session.position_panel
        s._check_reference_ready()
        if (p.editing_locked() or not p.is_fresh() or s.linked_mode_var.get()
                or s.close_in_progress or s.disconnect_in_progress):
            raise ValueError("请先停止单轴运动，并等待有效位置反馈。")
        if p.user_zero_set:
            raise ValueError("当前轴已有用户零点，无需恢复。")
        return s._require_node()

    def request_restore(self):
        s = self.session
        node = self._check_ready()
        axis, generation = s.active_axis_var.get(), s.cancel_generation
        key, record = self.key(axis), self.record(axis)
        if record is None:
            raise ValueError("本设备暂无可恢复的零点记录。")
        coordinate = self.coordinate(record)

        def accept():
            if (self._check_ready() is not node or s.active_axis_var.get() != axis
                    or s.cancel_generation != generation or self.key(axis) != key
                    or self.record(axis) != record):
                raise ValueError("设备或记录已变化，请重新点击恢复。")

            def restore():
                if (s.nodes.get(axis) is not node or s.active_axis_var.get() != axis
                        or s.cancel_generation != generation or self.key(axis) != key
                        or s.position_panel.user_zero_set):
                    raise RuntimeError("恢复请求已失效。")
                s._capture_user_zero_worker(node, axis, coordinate)
                if s.limit_enabled_var.get():
                    s._post("log", "恢复零点不修改自定义限位；若编码器基准变化，请核对工作区间。")

            s._safe_start("恢复上一次零点位置", restore)

        s.view.confirm("恢复上一次零点位置",
                       self.description() + "\n请确认是同一推进器，当前位置仍对应上述用户位置。\n"
                       "程序异常退出时，记录可能早于实际停止位置。\n"
                       "请松手：采样会短暂开启驱动，完成后恢复坐标，不执行返回零点运动。", accept)
