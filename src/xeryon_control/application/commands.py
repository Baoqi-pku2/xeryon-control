"""Admission, serialization and cancellation of background device commands.

The host supplies the session state, the synchronous command-start notification
and the event sink. Device operations always run under the session I/O lock.
"""
from __future__ import annotations

import threading
from typing import Callable

from canopen import BaseNode402

from xeryon_control.application.motion import StartConfirmation
from xeryon_control.hardware.canopen_io import error_handler, set_controlword, set_target_position


class CommandDispatcher:
    def _init_command_dispatch(self):
        self.io_lock = threading.RLock()
        self.controlword_lock = threading.Lock()
        self.test_stop_requested = threading.Event()
        self.command_busy = False
        self.cancel_generation = 0

    def _run_command(self, label: str, func: Callable[[], object]) -> None:
        if self.__dict__.get("start_confirmation") is not None and label != "Disconnect":
            raise RuntimeError("请先继续或取消待确认的运动。")
        if label not in ("Disconnect", "Connect", "复核零点"):
            self._check_reference_ready()
        if label != "Disconnect" and (self.__dict__.get("close_in_progress", False)
                                      or self.__dict__.get("disconnect_in_progress", False)):
            raise RuntimeError("正在停止并断开设备，不能提交新命令。")
        if self.stop_pending:
            raise RuntimeError("正在确认停止，请等待。")
        if self.stop_unconfirmed and label not in ("Disconnect", "Connect"):
            raise RuntimeError("上次停止未确认，禁止新命令。请确认硬件已停止后断开并重新连接。")
        shortcut_running = self.__dict__.get("shortcut_active") is not None or bool(self.__dict__.get("shortcut_pending"))
        if shortcut_running and label != "Disconnect" and not label.startswith("快捷 "):
            raise RuntimeError("快捷流程进行中：请等待完成，或使用 STOP 停止后再操作。")
        if self.active_target_inc is not None and label != "Disconnect":
            raise RuntimeError("运动进行中：请等待到位，或使用 STOP 停止并确认失能后再操作。")
        if self.command_busy:
            raise RuntimeError(f"{label} 未提交：另一条命令仍在执行，请等待完成。")

        self.command_busy = True
        self._on_command_started(label)
        thread = threading.Thread(target=self._command_runner, args=(label, func, self.cancel_generation), daemon=True)
        thread.start()
        self._log(f"{label} 已提交后台执行。")

    def _command_runner(self, label: str, func: Callable[[], object], generation=None) -> None:
        completion = ("busy", "Idle")
        try:
            with self.io_lock:
                if generation is not None and generation != self.cancel_generation:
                    raise RuntimeError("命令已被 STOP 取消")
                if label not in ("Disconnect", "Connect", "复核零点"):
                    self._check_reference_ready()
                self._motion_generation = generation
                result = func()
            if isinstance(result, StartConfirmation):
                completion = ("review_start", result)
            else:
                self._post("log", f"{label} complete.")
        except Exception as exc:
            error_handler(exc)
            self._post("log", f"{label} failed: {exc}")
            self._post("error", str(exc))
        finally:
            self._post(*completion)

    def _check_motion_cancelled(self):
        self._check_reference_ready()
        generation = self.__dict__.get("_motion_generation")
        if ((generation is not None and generation != self.cancel_generation)
                or self.test_stop_requested.is_set() or self.__dict__.get("stop_unconfirmed", False)):
            raise RuntimeError("运动已取消或停止状态未确认")

    def _reset_motion_cancel(self):
        self._check_reference_ready()
        if self.stop_pending or self.stop_unconfirmed:
            raise RuntimeError("停止处理中或停止状态未确认，不能开始运动")
        self.test_stop_requested.clear()

    def _write_controlword(self, node: BaseNode402, value: int) -> None:
        with self.controlword_lock:
            if value & 0x04 and value & 0x08:
                self._check_motion_cancelled()
            set_controlword(node, value)

    def _write_target_position(self, node: BaseNode402, value: int) -> None:
        with self.controlword_lock:
            self._check_motion_cancelled()
            set_target_position(node, value)
