"""Independent STOP lane: broadcast disable first, then verify under the I/O lock."""
from __future__ import annotations

import threading
from typing import Callable

from canopen import BaseNode402

from xeryon_control.hardware.stop import verify_disabled


class StopCoordinator:
    def _init_stop_control(self):
        self.stop_pending = False
        self.stop_verified = False
        self.stop_unconfirmed = False
        self.stop_deadline_seconds = 3.0

    def _request_stop(self, label: str, *, notify: Callable[[str], None]) -> tuple[int, float] | None:
        if self.stop_pending:
            return
        if self.__dict__.get("close_in_progress") and label != "Close":
            return
        if not self.nodes:
            notify("未连接设备 · 无法确认停止，请使用硬件停止")
            return
        self.stop_verified = False
        self.stop_pending = True
        self.cancel_generation += 1
        self.test_stop_requested.set()
        notify("正在停止 · 等待设备确认")
        self._log(f"{label} requested.")
        thread = threading.Thread(target=self._stop_runner, args=(label,), daemon=True)
        thread.start()
        self.stop_deadline_seconds = max(3.0, 2.0 + 1.5 * len(self.nodes))
        return self.cancel_generation, self.stop_deadline_seconds

    def _stop_timed_out(self, generation):
        if self.stop_pending and generation == self.cancel_generation:
            self.stop_unconfirmed = True
            return True
        return False

    def _stop_runner(self, label: str) -> None:
        try:
            # RPDO disable must not wait behind a long-running SDO/command lock.
            # Each send is serialized with motion writes; cancellation gates all
            # subsequent enable/setpoint writes, so a worker cannot undo STOP.
            for node in list(self.nodes.values()):
                try:
                    self._write_controlword(node, 0x00)
                except Exception as exc:
                    self._post("log", f"Immediate stop send failed: {exc}")
            if not self.io_lock.acquire(timeout=2.0):
                raise RuntimeError("通信线程未及时释放，停止状态未确认")
            try:
                # Every UI stop route uses the same verified disable path.
                self._stop_and_disable_worker()
                self.stop_verified = True
            finally:
                self.io_lock.release()
            self._post("log", f"{label} complete.")
        except Exception as exc:
            self.stop_unconfirmed = True
            self._post("target", "停止未确认 · 请关闭驱动器电源")
            self._post("log", f"{label} failed: {exc}")
            self._post("error", f"停止未确认，请关闭驱动器电源。\n{exc}")
        finally:
            self.stop_pending = False

    def _stop_and_disable_worker(self) -> None:
        if not self.nodes:
            return
        self._disable_drive_worker()

    def _disable_drive_worker(self, *, only_node=None) -> dict[int, dict[str, int]]:
        try:
            nodes = [only_node] if only_node is not None else list(self.nodes.values()) or [self._require_node()]
            result = self._disable_nodes(nodes)
            self._record_motion_event("drive_disabled")
        except Exception as exc:
            self.stop_unconfirmed = True
            self._record_motion_event("disable_failed", error=str(exc))
            self._post("target", "停止未确认 · 请关闭驱动器电源")
            raise
        else:
            self.stop_unconfirmed = False
            if isinstance(result, dict):
                for axis, node in self.nodes.items():
                    if node.id in result:
                        count = int(result[node.id]["position_inc"])
                        self.__dict__.setdefault("reference_positions", {})[axis] = count
                        if axis == self._active_axis_name():
                            self.last_position_inc = count
            self._post("target", "已停止 · 已核对失能与位置稳定")
            return result
        finally:
            self.active_target_inc = None
            self.active_start_inc = None

    def _verify_disabled_target(self, node, target_inc: int, result: object) -> None:
        if not isinstance(result, dict):
            return
        stopped = result.get(node.id)
        if stopped is None:
            return
        remaining = abs(int(stopped["position_inc"]) - int(target_inc))
        if remaining > max(1, self.runtime_position_window_inc):
            raise RuntimeError(
                f"失能后终点偏离目标 {self._inc_to_um(remaining):.2f} µm，"
                f"status=0x{int(stopped['statusword']):04X}"
            )

    def _quick_stop_nodes(self, nodes: list[BaseNode402]) -> None:
        errors = []
        for node in nodes:
            try:
                self._write_controlword(node, 0x0B)
            except Exception as exc:
                errors.append(f"node {node.id}: {exc}")
        if errors:
            raise RuntimeError("Quick stop failed for " + "; ".join(errors))

    def _disable_nodes(self, nodes: list[BaseNode402]) -> dict[int, dict[str, int]]:
        errors = []
        results = {}
        # Disable every axis before waiting for any individual acknowledgement.
        for node in nodes:
            try:
                self._write_controlword(node, 0x00)
            except Exception as exc:
                self._post("log", f"Node {node.id}: RPDO disable failed: {exc}")
        for node in nodes:
            try:
                result = verify_disabled(node)
                results[node.id] = result
                self._record_motion_event("stop_verified", node_id=node.id, **result)
            except Exception as exc:
                errors.append(f"node {node.id}: {exc}")
        if errors:
            self.stop_unconfirmed = True
            raise RuntimeError("Drive disable failed for " + "; ".join(errors))
        return results
