"""Two-axis motion using one session's shared command, STOP and device operations."""
from __future__ import annotations

from math import isfinite
import time
from typing import Callable, Optional

from canopen import BaseNode402
from xeryon_control.application.feedback import MOTION_PROGRESS_EPSILON_UM, MOTION_STALL_TIMEOUT_SECONDS
from xeryon_control.hardware.canopen_io import BIT


PAIR_MISMATCH_TIMEOUT_SECONDS = 1.0


class LinkedMotion:
    def _move_linked_absolute_worker(
        self,
        target_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        self._validate_target(target_um, limits)
        if bool(limits["enabled"]):
            self._apply_linked_limits_worker(limits, linked)
        axes = self._require_linked_axes(linked)
        starts = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        targets = self._linked_targets_for_logical_position(target_um, linked)
        distance_um = max(self._inc_to_um(abs(targets[axis] - starts[axis])) for axis in targets)
        timeout = self._manual_motion_timeout(distance_um, profile)
        result = self._run_linked_move(
            targets,
            profile,
            timeout,
            linked,
            starts_inc=starts,
            sync_mode="position",
            target_label=f"Linked target {target_um:.2f} um",
        )
        if result["status"] != "OK":
            raise RuntimeError(str(result["status"]))

    def _move_linked_relative_worker(
        self,
        delta_um: float,
        profile: dict[str, float | int],
        limits: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        axes = self._require_linked_axes(linked)
        starts = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        delta_inc = self._um_to_inc(delta_um)
        targets = {
            axis: starts[axis] + direction * delta_inc
            for axis, _, direction in axes
        }

        offsets = dict(linked["offsets"])
        for axis, _, direction in axes:
            if axis in offsets:
                logical_target = direction * self._inc_to_um(targets[axis] - int(offsets[axis]))
                self._validate_target(logical_target, limits)
            else:
                self._validate_target(self._inc_to_um(targets[axis]), limits)
        if bool(limits["enabled"]) and all(axis in offsets for axis, _, _ in axes):
            self._apply_linked_limits_worker(limits, linked)

        result = self._run_linked_move(
            targets,
            profile,
            self._manual_motion_timeout(abs(delta_um), profile),
            linked,
            starts_inc=starts,
            sync_mode="travel",
            target_label=f"Linked relative {delta_um:+.2f} um",
        )
        if result["status"] != "OK":
            raise RuntimeError(str(result["status"]))

    def _run_linked_actuator_test_worker(
        self,
        test_config: dict[str, object],
        linked: dict[str, object],
    ) -> None:
        speeds = list(test_config["speeds"])
        distances = list(test_config["distances"])
        settle_time = float(test_config["settle_time"])
        step_timeout = float(test_config["step_timeout"])
        return_after = bool(test_config["return_after"])
        base_profile = dict(test_config["profile"])
        axes = self._require_linked_axes(linked)
        step_no = 1
        self._post("log", "Starting linked actuator test on A+B.")

        def run_step(speed: float, distance: float, number: int) -> dict[str, object]:
            profile = dict(base_profile)
            profile["velocity"] = speed
            profile["velocity_inc"] = self._um_to_inc(speed)
            if speed <= 0 or speed > float(profile["max_velocity"]):
                raise ValueError("双轴测试速度必须大于 0 且不能超过配置的最大速度。")
            if profile["velocity_inc"] < 1:
                raise ValueError("双轴测试速度量化后为 0 计数/秒，请提高速度或核对编码器型号。")
            starts = {
                axis: self._read_actual_position_inc(node, fresh=True)
                for axis, node, _ in axes
            }
            delta_inc = self._um_to_inc(distance)
            targets = {
                axis: starts[axis] + direction * delta_inc
                for axis, _, direction in axes
            }
            offsets = dict(linked["offsets"])
            for axis, _, direction in axes:
                if axis in offsets:
                    logical_target = direction * self._inc_to_um(targets[axis] - int(offsets[axis]))
                    self._validate_target(logical_target, dict(profile["limits"]))
                else:
                    self._validate_target(self._inc_to_um(targets[axis]), dict(profile["limits"]))
            result = self._run_linked_move(
                targets,
                profile,
                step_timeout,
                linked,
                starts_inc=starts,
                sync_mode="travel",
                target_label=f"Linked test step {number}",
            )
            for axis, values in dict(result["axes"]).items():
                self._post(
                    "test_result",
                    {
                        "step": number,
                        "axis": axis,
                        "speed": speed,
                        "command": float(values["command"]),
                        "actual": float(values["actual"]),
                        "error": float(values["error"]),
                        "duration": float(result["duration"]),
                        "status": result["status"],
                    },
                )
            return result

        for speed in speeds:
            for distance in distances:
                if self.test_stop_requested.is_set():
                    self._post("log", "Linked actuator test stopped before next step.")
                    return
                result = run_step(float(speed), float(distance), step_no)
                if result["status"] != "OK":
                    raise RuntimeError(str(result["status"]))
                step_no += 1
                if self.test_stop_requested.wait(settle_time):
                    return

                if return_after:
                    if self.test_stop_requested.is_set():
                        self._post("log", "Linked actuator test stopped before reverse step.")
                        return
                    result = run_step(float(speed), -float(distance), step_no)
                    if result["status"] != "OK":
                        raise RuntimeError(str(result["status"]))
                    step_no += 1
                    if self.test_stop_requested.wait(settle_time):
                        return

        self._post("log", "Linked actuator test complete.")

    def _run_linked_move(
        self,
        targets_inc: dict[str, int],
        profile: dict[str, float | int],
        step_timeout: float,
        linked: dict[str, object],
        *,
        starts_inc: Optional[dict[str, int]] = None,
        sync_mode: str = "travel",
        disable_after: bool = True,
        target_label: str = "Linked move",
        before_setpoint: Optional[Callable[[], None]] = None,
    ) -> dict[str, object]:
        axes = self._require_linked_axes(linked)
        nodes = [node for _, node, _ in axes]
        starts = dict(starts_inc or {})
        for axis, node, _ in axes:
            starts.setdefault(axis, self._read_actual_position_inc(node, fresh=True))
            if axis not in targets_inc:
                raise ValueError(f"Missing linked target for {axis}.")

        currents = dict(starts)
        statuswords: dict[str, int] = {}
        best_remaining = {
            axis: abs(int(targets_inc[axis]) - starts[axis])
            for axis, _, _ in axes
        }
        last_progress = {axis: time.monotonic() for axis, _, _ in axes}
        progress_samples_by_axis = {axis: [best_remaining[axis]] * 2 for axis, _, _ in axes}
        mismatch_since: Optional[float] = None
        status = "OK"
        start_time = time.monotonic()

        self._validate_linked_targets(targets_inc, linked)
        self.runtime_position_window_inc = max(1, int(profile.get("position_window_inc", self.__dict__.get("runtime_position_window_inc", 1))))
        self.active_target_inc = None
        self.active_start_inc = None

        if bool(dict(profile.get("limits", {})).get("enabled")):
            self._apply_linked_limits_worker(dict(profile["limits"]), linked)
        try:
            for axis, node, _ in axes:
                self._prepare_profile_position(node, profile, starts[axis])
            for axis, node, _ in axes:
                statuswords[axis] = self._read_statusword(node, fresh=True)

            if before_setpoint is not None:
                before_setpoint()
            self._command_profile_positions(
                [(node, int(targets_inc[axis])) for axis, node, _ in axes]
            )
            self._post("target", target_label)
            self._post(
                "target_start",
                " / ".join(f"{axis[-1]} {self._inc_to_um(starts[axis]):.2f}" for axis, _, _ in axes),
            )
            self._post("target_moved", "A 0.00 / B 0.00")
            self._post("target_progress", "0.0%")

            deadline = time.monotonic() + step_timeout
            while time.monotonic() < deadline:
                if self.test_stop_requested.is_set():
                    status = "Stopped"
                    break

                for axis, node, _ in axes:
                    try:
                        node.tpdo[3].wait_for_reception(timeout=0.02)
                    except Exception:
                        pass
                    currents[axis] = self._read_actual_position_inc(
                        node,
                        fallback=currents[axis],
                    )
                    statuswords[axis] = self._read_statusword(
                        node,
                        fallback=statuswords[axis],
                    )

                now = time.monotonic()
                remaining = {
                    axis: abs(int(targets_inc[axis]) - currents[axis])
                    for axis, _, _ in axes
                }
                travelled = {
                    axis: direction * self._inc_to_um(currents[axis] - starts[axis])
                    for axis, _, direction in axes
                }
                totals = {
                    axis: direction * self._inc_to_um(int(targets_inc[axis]) - starts[axis])
                    for axis, _, direction in axes
                }
                progress = [
                    100.0 if totals[axis] == 0 else travelled[axis] / totals[axis] * 100.0
                    for axis, _, _ in axes
                ]
                pair_error = self._linked_pair_error_um(
                    currents,
                    starts,
                    linked,
                    sync_mode,
                )
                self._post("target_moved", f"A {travelled['Actuator A']:.2f} / B {travelled['Actuator B']:.2f}")
                self._post("target_remaining", f"{max(self._inc_to_um(value) for value in remaining.values()):.2f}")
                self._post("target_progress", f"{max(0.0, min(100.0, min(progress))):.1f}%")
                self._post("linked_status", self._linked_status_payload(currents, linked, pair_error))

                fault_axis = next(
                    (axis for axis, _, _ in axes if statuswords[axis] & (BIT(3) | BIT(13))),
                    None,
                )
                if fault_axis is not None:
                    self._quick_stop_nodes(nodes)
                    fault = "drive fault" if statuswords[fault_axis] & BIT(3) else "following error"
                    status = f"{fault_axis} {fault}"
                    break

                stalled_axis = None
                for axis, _, _ in axes:
                    best_remaining[axis], last_progress[axis], stalled = self._update_progress_watchdog(
                        best_remaining[axis],
                        self._progress_sample(progress_samples_by_axis[axis], remaining[axis]),
                        last_progress[axis],
                        now,
                        max(1, self._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
                        MOTION_STALL_TIMEOUT_SECONDS,
                    )
                    if stalled:
                        stalled_axis = axis
                        break
                if stalled_axis is not None:
                    self._quick_stop_nodes(nodes)
                    status = f"{stalled_axis} stalled"
                    break

                if pair_error > float(linked["tolerance_um"]):
                    mismatch_since = mismatch_since or now
                    if now - mismatch_since >= PAIR_MISMATCH_TIMEOUT_SECONDS:
                        self._quick_stop_nodes(nodes)
                        status = f"Pair mismatch {pair_error:.2f} um"
                        break
                else:
                    mismatch_since = None

                reached = [
                    bool(statuswords[axis] & BIT(10))
                    and remaining[axis] <= max(1, self.runtime_position_window_inc)
                    for axis, _, _ in axes
                ]
                if all(reached):
                    break
                time.sleep(0.08)
            else:
                if self.test_stop_requested.is_set():
                    status = "Stopped"
                else:
                    self._quick_stop_nodes(nodes)
                    status = "Timeout"

            if self.test_stop_requested.is_set():
                status = "Stopped"
            else:
                for axis, node, _ in axes:
                    currents[axis] = self._read_actual_position_inc(node, fresh=True)
                    statuswords[axis] = self._read_statusword(node, fresh=True)
            if status == "OK":
                fault_axis = next(
                    (axis for axis, _, _ in axes if statuswords[axis] & (BIT(3) | BIT(13))),
                    None,
                )
                if fault_axis is not None:
                    raise RuntimeError(f"{fault_axis} fault during final verification")
                for axis, _, _ in axes:
                    remaining = abs(int(targets_inc[axis]) - currents[axis])
                    if not statuswords[axis] & BIT(10) or remaining > max(1, self.runtime_position_window_inc):
                        raise RuntimeError(
                            f"{axis} endpoint not confirmed: remaining {self._inc_to_um(remaining):.2f} um, "
                            f"status=0x{statuswords[axis]:04X}"
                        )
                final_pair_error = self._linked_pair_error_um(currents, starts, linked, sync_mode)
                if final_pair_error > float(linked["tolerance_um"]):
                    raise RuntimeError(f"Final pair mismatch {final_pair_error:.2f} um exceeds tolerance")
        except Exception as exc:
            status = f"Error: {exc}"
            if not self.test_stop_requested.is_set():
                try:
                    self._quick_stop_nodes(nodes)
                except Exception:
                    pass
        finally:
            # STOP owns verified cleanup after cancelling this worker. Repeating
            # multi-axis SDO verification here would keep io_lock from STOP.
            if not self.test_stop_requested.is_set() and (disable_after or status != "OK"):
                try:
                    disabled_result = self._disable_nodes(nodes)
                    if status == "OK":
                        for axis, node, _ in axes:
                            self._verify_disabled_target(node, int(targets_inc[axis]), disabled_result)
                except Exception as exc:
                    status = f"{status}; disable error: {exc}"

        pair_error = self._linked_pair_error_um(currents, starts, linked, sync_mode)
        axis_results = {}
        for axis, _, direction in axes:
            command_um = direction * self._inc_to_um(int(targets_inc[axis]) - starts[axis])
            actual_um = direction * self._inc_to_um(currents[axis] - starts[axis])
            axis_results[axis] = {
                "command": command_um,
                "actual": actual_um,
                "error": actual_um - command_um,
            }

        duration = time.monotonic() - start_time
        self._post("linked_status", self._linked_status_payload(currents, linked, pair_error))
        self._post("target", f"{target_label}: {status}")
        self._post(
            "log",
            f"{target_label} {status}; mismatch={pair_error:.2f} um, duration={duration:.2f} s.",
        )
        return {
            "status": status,
            "duration": duration,
            "pair_error": pair_error,
            "axes": axis_results,
        }

    def _apply_linked_limits_worker(
        self,
        limits_config: dict[str, float | bool],
        linked: dict[str, object],
    ) -> None:
        axes = self._require_linked_axes(linked)
        if not bool(limits_config["enabled"]):
            for _, node, _ in axes:
                limits = node.sdo["Software position limit"]
                limits[1].raw = 0
                limits[2].raw = 0
            return

        offsets = dict(linked["offsets"])
        logical_min = float(limits_config["min"])
        logical_max = float(limits_config["max"])
        low_inc, high_inc = self._limit_count_bounds(logical_min, logical_max)
        for axis, node, direction in axes:
            offset = int(offsets[axis])
            endpoints = [
                offset + direction * low_inc,
                offset + direction * high_inc,
            ]
            limits = node.sdo["Software position limit"]
            limits[1].raw = min(endpoints)
            limits[2].raw = max(endpoints)
        self._post(
            "log",
            f"Applied linked logical limits {logical_min:.2f} to {logical_max:.2f} um.",
        )

    def _linked_snapshot(self, require_zero: bool = False) -> dict[str, object]:
        enabled = self._linked_mode_enabled()
        invert_b = self.invert_b_enabled
        snapshot: dict[str, object] = {
            "enabled": enabled,
            "invert_b": invert_b,
            "tolerance_um": 0.0,
            "offsets": dict(self.linked_zero_offsets_inc),
        }
        if not enabled:
            return snapshot

        tolerance = self.pair_tolerance_um
        if not isfinite(tolerance) or tolerance <= 0:
            raise ValueError("双轴最大位置差必须为大于 0 的有限数值。")
        snapshot["tolerance_um"] = tolerance

        missing = [axis for axis in ("Actuator A", "Actuator B") if axis not in self.nodes]
        if missing:
            raise RuntimeError("A+B linked mode requires both Actuator A and Actuator B to be connected.")
        if require_zero and any(axis not in self.linked_zero_offsets_inc for axis in self.nodes):
            raise RuntimeError("Set Pair Zero before linked absolute moves or the Standard Cycle.")
        return snapshot

    def _require_linked_axes(
        self,
        linked: dict[str, object],
    ) -> list[tuple[str, BaseNode402, int]]:
        if not bool(linked["enabled"]):
            raise RuntimeError("A+B linked mode is not enabled.")
        try:
            node_a = self.nodes["Actuator A"]
            node_b = self.nodes["Actuator B"]
        except KeyError as exc:
            raise RuntimeError("A+B linked mode requires two connected actuators.") from exc
        return [
            ("Actuator A", node_a, 1),
            ("Actuator B", node_b, -1 if bool(linked["invert_b"]) else 1),
        ]

    def _set_linked_zero_worker(self) -> None:
        linked = self._linked_snapshot()
        axes = self._require_linked_axes(linked)
        positions = {
            axis: self._read_actual_position_inc(node, fresh=True)
            for axis, node, _ in axes
        }
        self.linked_zero_offsets_inc = positions
        linked["offsets"] = dict(positions)
        self._post("pair_zero_status", "Pair zero set")
        self._post("linked_status", self._linked_status_payload(positions, linked, 0.0))
        self._post(
            "log",
            "Linked software zero captured for A and B. Device home offsets were not changed.",
        )

    def _invalidate_linked_zero(self, reason: str) -> None:
        if not self.__dict__.get("linked_zero_offsets_inc", {}):
            return
        self.linked_zero_offsets_inc = {}
        self._post("pair_zero_status", "Not set")
        self._post("log", f"Linked software zero cleared after {reason}.")

    def _validate_linked_targets(self, targets_inc: dict[str, int], linked: dict[str, object]) -> None:
        for axis, _, _ in self._require_linked_axes(linked):
            travel = self.travel_ranges.get(axis)
            if travel is None or not travel.confirmed:
                raise ValueError(f"双轴 {axis} 缺少有效的机械行程映射，已拒绝联动目标。")
            target_inc = int(targets_inc[axis])
            travel.validate(
                self._inc_to_um(target_inc),
                target_inc,
                self._inc_to_um(1),
                axis,
            )

    def _linked_targets_for_logical_position(
        self,
        target_um: float,
        linked: dict[str, object],
    ) -> dict[str, int]:
        offsets = dict(linked["offsets"])
        target_inc = self._um_to_inc(target_um)
        return {
            axis: int(offsets[axis]) + direction * target_inc
            for axis, _, direction in self._require_linked_axes(linked)
        }

    def _read_linked_logical_positions(
        self,
        linked: dict[str, object],
        *,
        fresh: bool = False,
    ) -> dict[str, float]:
        offsets = dict(linked["offsets"])
        return {
            axis: direction
            * self._inc_to_um(self._read_actual_position_inc(node, fresh=fresh) - int(offsets[axis]))
            for axis, node, direction in self._require_linked_axes(linked)
        }

    def _linked_pair_error_um(
        self,
        currents: dict[str, int],
        starts: dict[str, int],
        linked: dict[str, object],
        sync_mode: str,
    ) -> float:
        axes = self._require_linked_axes(linked)
        if sync_mode == "position":
            offsets = dict(linked["offsets"])
            values = {
                axis: direction * self._inc_to_um(currents[axis] - int(offsets[axis]))
                for axis, _, direction in axes
            }
        else:
            values = {
                axis: direction * self._inc_to_um(currents[axis] - starts[axis])
                for axis, _, direction in axes
            }
        return abs(values["Actuator A"] - values["Actuator B"])

    def _linked_status_payload(
        self,
        currents: dict[str, int],
        linked: dict[str, object],
        pair_error: Optional[float] = None,
    ) -> dict[str, object]:
        axes = self._require_linked_axes(linked)
        offsets = dict(linked["offsets"])
        if all(axis in offsets for axis, _, _ in axes):
            positions = {
                axis: direction * self._inc_to_um(currents[axis] - int(offsets[axis]))
                for axis, _, direction in axes
            }
        else:
            positions = {
                axis: self._inc_to_um(currents[axis])
                for axis, _, _ in axes
            }
        return {
            "position_a": positions["Actuator A"],
            "position_b": positions["Actuator B"],
            "pair_error": (
                abs(positions["Actuator A"] - positions["Actuator B"])
                if pair_error is None and all(axis in offsets for axis, _, _ in axes)
                else pair_error
            ),
        }
