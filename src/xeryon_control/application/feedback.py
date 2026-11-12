"""Background encoder polling, measured feedback and motion supervision."""
from __future__ import annotations

import threading
import time
from typing import Optional

from canopen import BaseNode402
from canopen.sdo.exceptions import SdoAbortedError
from xeryon_control.hardware.canopen_io import BIT
from xeryon_control.hardware.parameters import NodeOperationMode


MOTION_PROGRESS_EPSILON_UM = 1.0
MOTION_STALL_TIMEOUT_SECONDS = 10.0
PDO_MAX_AGE_SECONDS = 1.0
CONTROLLER_TRACE_INTERVAL_SECONDS = 2.0


def start_poll_if_due(session) -> None:
    if (session.node is not None and not session.status_busy and not session.command_busy
            and not session.stop_pending and not session.stop_unconfirmed
            and time.monotonic() >= session.__dict__.get("next_status_poll_time", 0.0)
            and not session.__dict__.get("close_in_progress", False)
            and not session.__dict__.get("disconnect_in_progress", False)):
        session.status_busy = True
        thread = threading.Thread(target=session._status_worker, daemon=True)
        thread.start()


def poll_feedback(session, *, rest_seconds) -> None:
    try:
        with session.io_lock:
            _sample_and_supervise(session)
    except Exception as exc:
        session._post("log", f"Status read failed: {exc}")
        if session.__dict__.get("reference_review") is None:
            session.reference_review = dict(session.__dict__.get("reference_positions", {}))
        session.cancel_generation = session.__dict__.get("cancel_generation", 0) + 1
        session._post("feedback_stale", None)
        session._post("connection_health", False)
        with session.io_lock:
            session._record_motion_event("communication_fault", error=str(exc))
            session.test_stop_requested.set()
            stop_errors = []
            try:
                session._disable_nodes(list(session.nodes.values()))
            except Exception as stop_exc:
                stop_errors.append(str(stop_exc))
            session.stop_unconfirmed = bool(stop_errors)
            session.active_target_inc = None
            session.active_start_inc = None
            message = "通信异常：已请求停止并关闭驱动" if not stop_errors else "通信异常：停止状态未确认，请使用硬件停止"
            session._record_motion_event("communication_stop", confirmed=not stop_errors, errors=stop_errors)
            session._post("target", message)
            session._post("log", message)
            session._post("error", message)
            session._post_shortcut_failure(message)
    finally:
        # Reserve a real idle window after I/O, even when a read takes longer
        # than the poll timer. This prevents status traffic starving commands.
        session.next_status_poll_time = time.monotonic() + rest_seconds
        session.status_busy = False


def _sample_and_supervise(session):
    feedback_axis = session._active_axis_name()
    node = session.nodes.get(feedback_axis, session.node)
    if node is None:
        return
    session.node = node

    # A recently received PDO can still repeat an old position.
    # While idle, display the result of a new 0x6064 upload each poll.
    idle = session.active_target_inc is None
    if not idle:
        try:
            node.tpdo[3].wait_for_reception(timeout=0.05)
        except Exception:
            pass

    position_inc = session._read_actual_position_inc(node, fresh=idle)
    sample_time = time.monotonic()
    statusword = session._read_statusword(node)
    state = str(getattr(node, "state", "-"))
    mode = session._read_mode(node)
    session.last_position_inc = position_inc
    session.last_statusword = statusword

    status = {
        "axis": feedback_axis,
        "sample_time": sample_time,
        "position_um": session._inc_to_um(position_inc),
        "position_inc": position_inc,
        "statusword": statusword,
        "state": state,
        "mode": mode,
    }

    sample_positions = {feedback_axis: position_inc}
    if "Actuator A" in session.nodes and "Actuator B" in session.nodes:
        linked = {
            "enabled": True,
            "invert_b": bool(session.__dict__.get("invert_b_enabled", False)),
            "tolerance_um": 0.0,
            "offsets": dict(session.linked_zero_offsets_inc),
        }
        pair_currents = {
            axis: position_inc if pair_node is node else session._read_actual_position_inc(pair_node, fresh=idle)
            for axis, pair_node, _ in session._require_linked_axes(linked)
        }
        sample_positions = pair_currents
        session._post("linked_status", session._linked_status_payload(pair_currents, linked))

    if session.active_target_inc is None and session.__dict__.get("shortcut_wait_deadline") is not None:
        _check_dwell_position(session, node, position_inc, statusword)
    if session.active_target_inc is not None:
        _supervise_target(session, node, status)

    session.reference_positions = sample_positions
    session._post("connection_health", True)
    session._post("status", status)


def _check_dwell_position(session, node, position_inc, statusword):
    step = session.__dict__.get("shortcut_active")
    if step is not None:
        expected = session._um_to_inc(float(step["target"]))
        window = max(1, session.runtime_position_window_inc)
        if statusword & (BIT(3) | BIT(13)) or abs(position_inc - expected) > window:
            current = session._read_actual_position_inc(node, fresh=True)
            live_status = session._read_statusword(node, fresh=True)
            if live_status & (BIT(3) | BIT(13)) or abs(current - expected) > window:
                session.test_stop_requested.set()
                session._disable_drive_worker()
                reason = "停留期间驱动故障或位置偏离，已取消后续步骤"
                session._post("target", reason)
                session._post("log", reason)
                session._post_shortcut_failure(reason)


def _supervise_target(session, node, status):
    position_inc = status["position_inc"]
    statusword = status["statusword"]
    now = time.monotonic()
    if session.active_start_inc is not None:
        session._update_target_progress(position_inc)

    remaining_inc = abs(position_inc - session.active_target_inc)
    software_stall = False
    candidate_best = session.active_best_remaining_inc
    candidate_last = session.active_last_progress_time
    progress_samples = session.__dict__.setdefault('active_progress_samples', [])
    progress_remaining = session._progress_sample(progress_samples, remaining_inc)
    if session.active_best_remaining_inc is not None:
        (
            candidate_best,
            candidate_last,
            software_stall,
        ) = session._update_progress_watchdog(
            session.active_best_remaining_inc,
            progress_remaining,
            session.active_last_progress_time,
            now,
            max(1, session._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
            MOTION_STALL_TIMEOUT_SECONDS,
        )

    target_window = session.runtime_position_window_inc
    target_reached = bool(statusword & BIT(10))
    close_enough = remaining_inc <= max(1, target_window)
    overshot = session._target_overshot(position_inc)
    if overshot or software_stall or (target_reached and close_enough) or statusword & (BIT(3) | BIT(13)):
        # Verify a terminal decision against the live device, never only a PDO cache.
        position_inc = session._read_actual_position_inc(node, fresh=True)
        statusword = session._read_statusword(node, fresh=True)
        now = time.monotonic()
        remaining_inc = abs(position_inc - session.active_target_inc)
        (candidate_best, candidate_last,
         software_stall) = session._update_progress_watchdog(
            session.active_best_remaining_inc,
            session._progress_sample(progress_samples, remaining_inc, replace_last=True),
            session.active_last_progress_time, now,
            max(1, session._um_to_inc(MOTION_PROGRESS_EPSILON_UM)),
            MOTION_STALL_TIMEOUT_SECONDS,
        )
        target_reached = bool(statusword & BIT(10))
        close_enough = remaining_inc <= max(1, target_window)
        overshot = session._target_overshot(position_inc)
        session.last_position_inc = position_inc
        session._update_target_progress(position_inc)
        status.update(position_um=session._inc_to_um(position_inc), position_inc=position_inc, statusword=statusword,
                      sample_time=now)
        session._record_motion_event("live_verification", position_inc=position_inc,
                                  statusword=statusword, stalled=software_stall)

    session.active_best_remaining_inc = candidate_best
    session.active_last_progress_time = candidate_last
    session._record_motion_event("sample", position_inc=position_inc, statusword=statusword,
                              remaining_inc=session.active_target_inc - position_inc)
    if statusword & (BIT(3) | BIT(13)):
        reason = "驱动故障" if statusword & BIT(3) else "跟随误差保护"
        session._record_motion_event("stop", reason=reason, position_inc=position_inc,
                                  statusword=statusword)
        session._disable_drive_worker()
        session._post("target", reason)
        session._post("log", f"{reason}，运动已停止，驱动已关闭；status=0x{statusword:04X}。")
        session._post_shortcut_failure(reason)
    elif overshot:
        session._record_motion_event("stop", reason="passed_target", position_inc=position_inc,
                                  target_inc=session.active_target_inc, statusword=statusword)
        session._disable_drive_worker()
        session._post("target", "位置越过目标，序列已中止")
        session._post("log", "位置越过目标和到位窗口。已停止并关闭驱动，请检查轨迹。")
        session._post_shortcut_failure("越过目标保护")
    elif target_reached and close_enough and (now - session.active_target_time) > 0.1:
        reached_um = session._inc_to_um(position_inc)
        session._record_motion_event("target_reached", position_inc=position_inc,
                                  statusword=statusword)
        target_inc = session.active_target_inc
        disabled_result = session._disable_drive_worker()
        session._verify_disabled_target(node, target_inc, disabled_result)
        session._post("single_axis_target", ("已到达", reached_um))
        session._post("target_remaining", "0.00")
        session._post("target_progress", "100.0%")
        session._post("log", f"Target reached at {reached_um:.2f} um.")
        generation = session._shortcut_generation_token()
        if generation is not None:
            session._post("shortcut_done", generation)
    elif software_stall:
        remaining_um = session._inc_to_um(session.active_target_inc - position_inc)
        session._record_motion_event("stop", reason="no_progress_live_confirmed",
                                  position_inc=position_inc, statusword=statusword,
                                  remaining_um=remaining_um)
        session._disable_drive_worker()
        session._post("target", "无进展保护（已实时复核）")
        session._post(
            "log",
            f"Motion halted after {MOTION_STALL_TIMEOUT_SECONDS:.0f} s without target progress; "
            f"remaining {remaining_um:.2f} um; live SDO confirmed, position "
            f"{session._inc_to_um(position_inc):.2f} um, status 0x{statusword:04X}.",
        )
        session._post_shortcut_failure("无进展保护")
    elif now >= session.active_target_deadline:
        session._record_motion_event("stop", reason="timeout", position_inc=position_inc)
        session._disable_drive_worker()
        session._post("target", "Motion timeout")
        session._post("log", "Motion halted and disabled: target timeout.")
        session._post_shortcut_failure("运动超时")
    else:
        # Capture BEFORE any halt resets the controller's demand position.
        # Terminal decisions above never wait for these additional reads.
        session._record_controller_snapshot(node)



class FeedbackMonitoring:
    def _update_target_progress(self, position_inc: int) -> None:
        if self.active_start_inc is None or self.active_target_inc is None:
            return

        moved_um = self._inc_to_um(position_inc - self.active_start_inc)
        remaining_um = self._inc_to_um(self.active_target_inc - position_inc)
        total_inc = self.active_target_inc - self.active_start_inc

        if total_inc:
            progress = ((position_inc - self.active_start_inc) / total_inc) * 100.0
            progress = max(0.0, min(100.0, progress))
        else:
            progress = 100.0

        self._post("target_moved", f"{moved_um:.2f}")
        self._post("target_remaining", f"{remaining_um:.2f}")
        self._post("target_progress", f"{progress:.1f}%")

    def _target_overshot(self, position_inc: int) -> bool:
        if self.active_target_inc is None or self.active_start_inc is None:
            return False
        distance = self.active_target_inc - self.active_start_inc
        if not distance:
            return False
        direction = 1 if distance > 0 else -1
        return direction * (position_inc - self.active_target_inc) > max(1, self.runtime_position_window_inc)

    def _read_actual_position_inc(
        self, node: BaseNode402, fresh: bool = False, fallback: Optional[int] = None
    ) -> int:
        if not fresh:
            try:
                tpdo = node.tpdo[3]
                if self._pdo_is_fresh(tpdo):
                    return int(tpdo["Position Actual Value"].raw)
            except Exception:
                pass
            # Never substitute a previous position when feedback has expired.
        return self._read_feedback_sdo(node, "Position Actual Value")

    def _read_statusword(
        self, node: BaseNode402, fresh: bool = False, fallback: Optional[int] = None
    ) -> int:
        if not fresh:
            try:
                tpdo = node.tpdo[3]
                if self._pdo_is_fresh(tpdo):
                    return int(tpdo["Statusword"].raw)
            except Exception:
                pass
        return self._read_feedback_sdo(node, "Statusword")

    def _read_feedback_sdo(self, node, name):
        try:
            return int(node.sdo[name].raw)
        except Exception:
            if self.__dict__.get("command_busy") and self.__dict__.get("reference_review") is None:
                self.reference_review = dict(self.__dict__.get("reference_positions", {}))
                self.test_stop_requested.set()
                self._post("command_feedback_fault", node)
            raise

    @staticmethod
    def _pdo_is_fresh(tpdo) -> bool:
        # python-can/slcan timestamps are wall-clock seconds, not monotonic time.
        timestamp = tpdo.timestamp
        return timestamp is not None and 0 <= time.time() - timestamp <= PDO_MAX_AGE_SECONDS

    def _record_controller_snapshot(self, node) -> None:
        now = time.monotonic()
        if self.motion_trace_path is None or now < self.controller_trace_next_time:
            return
        self.controller_trace_next_time = now + CONTROLLER_TRACE_INTERVAL_SECONDS
        values = {"read_started_monotonic_s": now, "host_target_inc": self.active_target_inc}
        fields = (("demand_inc", 0x6062), ("actual_inc", 0x6064),
                  ("following_error_inc", 0x60F4), ("target_inc", 0x607A),
                  ("error_register", 0x1001), ("manufacturer_status", 0x1002))
        # Bound additional bus occupancy; never retry indefinitely or hide a lost link.
        timeout, retries = node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES
        try:
            node.sdo.MAX_RETRIES = 1
            for name, index in fields:
                if self.test_stop_requested.is_set():
                    values["interrupted_by_stop"] = True
                    break
                if index in self.controller_trace_unsupported:
                    continue
                budget = 0.5 - (time.monotonic() - now)
                if budget <= 0:
                    values["read_budget_exhausted"] = True
                    break
                node.sdo.RESPONSE_TIMEOUT = min(timeout, 0.25, budget)
                try:
                    values[name] = int(node.sdo[index].raw)
                except SdoAbortedError as exc:
                    # Unsupported firmware objects are explicit missing evidence.
                    if exc.code not in (0x06020000, 0x06090011):
                        raise
                    self.controller_trace_unsupported.add(index)
                    values[name + "_unavailable"] = str(exc)
        except Exception as exc:
            values["read_error"] = str(exc)
            raise  # Existing communication-fault handling stops and disables the drive.
        finally:
            node.sdo.RESPONSE_TIMEOUT, node.sdo.MAX_RETRIES = timeout, retries
            values["read_finished_monotonic_s"] = time.monotonic()
            self._record_motion_event("controller_snapshot", **values)

    def _read_mode(self, node: BaseNode402) -> str:
        try:
            mode_value = int(node.sdo["Modes of Operation Display"].raw)
        except Exception:
            try:
                mode_value = int(node.sdo[0x6061].raw)
            except Exception:
                return "-"

        for mode in NodeOperationMode:
            if mode.value == mode_value:
                return mode.name
        return str(mode_value)

    @staticmethod
    def _update_progress_watchdog(
        best_remaining_inc: int,
        remaining_inc: int,
        last_progress_time: float,
        now: float,
        min_progress_inc: int,
        stall_timeout: float,
    ) -> tuple[int, float, bool]:
        if remaining_inc <= best_remaining_inc - min_progress_inc:
            return remaining_inc, now, False
        return best_remaining_inc, last_progress_time, now - last_progress_time >= stall_timeout

    @staticmethod
    def _progress_sample(history, remaining_inc, replace_last=False):
        # Filter only progress history. Arrival, overshoot, faults and limits
        # continue to use the unfiltered live position/status immediately.
        # One forward spike must not set an unreachable all-time best that
        # falsely stops a still-moving slow trajectory ten seconds later.
        if replace_last and history:
            history[-1] = remaining_inc
        else:
            history.append(remaining_inc)
        del history[:-3]
        return sorted(history)[len(history) // 2]
