"""Real file/queue integration; no CAN connection or hardware movement."""
import json
import logging
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from xeryon_control.reporting.logging_service import _RotatingSink, start_logging, shutdown_logging
from xeryon_control.reporting.traces import append_event, new_trace_path


class LoggingTests(unittest.TestCase):
    def setUp(self):
        shutdown_logging()
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)

    def tearDown(self):
        shutdown_logging()
        self.directory.cleanup()

    def test_ui_backend_and_command_exception_share_one_file_without_ui_replay(self):
        from xeryon_control.application.session import ControllerSession
        service = start_logging(self.folder, install_hooks=False)
        session = ControllerSession()
        session.view = Mock()
        session.schedule = Mock()
        session._refresh_ui_state = Mock()
        session._log("用户置零记录")
        session._post("log", "设备连接记录")
        logging.getLogger("can.driver").warning("底层报文失败")
        def fail():
            raise ValueError("模拟参数错误")
        session._command_runner("日志测试命令", fail)
        session._drain_ui_queue()
        self.assertTrue(service.flush())
        text = service.path.read_text(encoding="utf-8")
        self.assertEqual(text.count("用户置零记录"), 1)
        self.assertEqual(text.count("设备连接记录"), 1)
        for expected in ("底层报文失败", "Command failed: 日志测试命令", "Traceback", "ValueError: 模拟参数错误"):
            self.assertIn(expected, text)

    def test_trace_snapshot_order_and_shutdown_drain(self):
        service = start_logging(self.folder, install_hooks=False)
        path = new_trace_path()
        self.assertEqual(path.parent, service.path.parent / "motion")
        values = {"profile": {"velocity": 10}}
        append_event(path, "command", values)
        values["profile"]["velocity"] = 99
        for i in range(30):
            append_event(path, "sample", {"position_inc": i})
        append_event(path, "stop", {"reason": "operator"})
        shutdown_logging()
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["profile"]["velocity"], 10)
        self.assertEqual([r["position_inc"] for r in records[1:-1]], list(range(30)))
        self.assertEqual(records[-1]["event"], "stop")
        self.assertTrue(all(r["session_id"] == service.session_id for r in records))
        self.assertIn("Application logging stopped", service.path.read_text(encoding="utf-8"))

    def test_rotation_and_session_retention_leave_results_untouched(self):
        old = self.folder / "20000101_000000_000000_1"
        old.mkdir()
        (old / "runtime.log").write_text("old", encoding="utf-8")
        report = self.folder / "results.json"
        report.write_text("preserve", encoding="utf-8")
        service = start_logging(self.folder, install_hooks=False, max_bytes=512, backups=2, keep_sessions=1)
        for i in range(80):
            logging.getLogger("rotation").info("row %s %s", i, "x" * 80)
        self.assertTrue(service.flush())
        self.assertFalse(old.exists())
        self.assertEqual(report.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(len(list(service.path.parent.glob("runtime.log*"))), 3)
        self.assertIn("row 79", service.path.read_text(encoding="utf-8"))

    def test_pruning_old_runtime_logs_preserves_session_motion(self):
        old = self.folder / "20000101_000000_000000_1"
        motion = old / "motion" / "motion_old.jsonl"
        motion.parent.mkdir(parents=True)
        motion.write_text('{"event": "sample"}\n', encoding="utf-8")
        (old / "runtime.log").write_text("old", encoding="utf-8")
        service = start_logging(self.folder, install_hooks=False, keep_sessions=1)
        self.assertTrue(service.flush())
        self.assertFalse((old / "runtime.log").exists())
        self.assertEqual(motion.read_text(encoding="utf-8"), '{"event": "sample"}\n')

    def test_disk_failure_is_visible_and_does_not_kill_writer(self):
        blocker = self.folder / "not-a-directory"
        blocker.write_text("block", encoding="utf-8")
        service = start_logging(blocker, install_hooks=False)
        logging.getLogger("failure").error("still accepted")
        self.assertTrue(service.flush())
        self.assertTrue(any("不可写" in m for m in service.take_notices()))
        self.assertTrue(service.thread.is_alive())
        blocker.unlink()
        logging.getLogger("failure").info("recovered")
        self.assertTrue(service.flush())
        self.assertIn("recovered", service.path.read_text(encoding="utf-8"))

    def test_trace_failure_is_visible(self):
        service = start_logging(self.folder, install_hooks=False)
        blocker = self.folder / "blocked"
        blocker.write_text("block", encoding="utf-8")
        append_event(blocker / "motion.jsonl", "command", {})
        self.assertTrue(service.flush())
        self.assertTrue(any("运动轨迹写入失败" in m for m in service.take_notices()))
        self.assertIn("运动轨迹写入失败", service.path.read_text(encoding="utf-8"))

    def test_log_page_opens_current_folder_and_only_clears_display(self):
        from tests.qt_support import create_console
        service = start_logging(self.folder, install_hooks=False)
        window = create_console()
        try:
            window.session._log("保留文件记录")
            with patch("xeryon_control.application.session.os.startfile") as opened:
                window.advanced.open_logs.click()
                opened.assert_called_once_with(service.path.parent)
            window.advanced.clear_logs.click()
            self.assertEqual(window.advanced.log.toPlainText(), "")
            self.assertTrue(service.flush())
            self.assertIn("保留文件记录", service.path.read_text(encoding="utf-8"))
            self.assertIsNone(window.session.network)
        finally:
            window.finish_exit()
            window.deleteLater()

    def test_slow_full_log_queue_does_not_block_stop_waiting_for_sdo(self):
        from tests.test_stop_interrupt import StopInterruptTests
        from tests.test_hardening import make_app as original_make_app
        from xeryon_control.application.session import ControllerSession
        entered, release = threading.Event(), threading.Event()
        original_emit = _RotatingSink.emit
        def slow_emit(handler, record):
            entered.set()
            release.wait(10)
            return original_emit(handler, record)
        def make_app():
            app = original_make_app()
            app.ui_queue = queue.Queue()
            app.view = None
            app._post = ControllerSession._post.__get__(app)
            app._log = ControllerSession._log.__get__(app)
            return app
        with patch.object(_RotatingSink, "emit", slow_emit):
            service = start_logging(self.folder, install_hooks=False, capacity=8)
            try:
                self.assertTrue(entered.wait(2))
                for i in range(40):
                    logging.getLogger("overload").info("queued %s", i)
                with patch("tests.test_stop_interrupt.make_app", make_app):
                    StopInterruptTests().exercise_blocked_upload(reply_before_timeout=True)
                    StopInterruptTests().exercise_blocked_upload(reply_before_timeout=False)
                self.assertTrue(service.thread.is_alive())
                self.assertGreater(service.dropped, 0)
            finally:
                release.set()
            # The queue may still be full when the barrier is first submitted.
            shutdown_logging()
        self.assertIn("日志队列已满", service.path.read_text(encoding="utf-8"))

    def test_uncaught_exceptions_in_process_and_thread_are_saved(self):
        script = '''
import sys, threading
from xeryon_control.reporting.logging_service import start_logging
sys.stderr = None  # Windowed EXEs have no console stream.
start_logging(sys.argv[1])
def fail():
    raise RuntimeError("thread crash marker")
t = threading.Thread(target=fail, name="broken-worker")
t.start()
t.join()
raise ValueError("main crash marker")
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.folder)],
                                capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        files = list(self.folder.glob("*/runtime.log"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        for expected in ("broken-worker", "RuntimeError: thread crash marker", "ValueError: main crash marker", "Traceback"):
            self.assertIn(expected, text)

    def test_reinitialization_does_not_duplicate_handlers_or_keep_hooks(self):
        before = (sys.excepthook, threading.excepthook)
        first = start_logging(self.folder)
        self.assertIs(start_logging(self.folder), first)
        shutdown_logging()
        self.assertEqual((sys.excepthook, threading.excepthook), before)
        self.assertNotIn(first.handler, logging.getLogger().handlers)
        second = start_logging(self.folder)
        logging.getLogger("second").info("second startup")
        self.assertTrue(second.flush())
        self.assertEqual(second.path.read_text(encoding="utf-8").count("second startup"), 1)

    def test_process_exit_is_bounded_even_when_writer_never_returns(self):
        script = '''
import sys, threading
from xeryon_control.reporting.logging_service import _RotatingSink, start_logging
entered = threading.Event()
def blocked_write(self, record):
    entered.set()
    threading.Event().wait()
_RotatingSink.emit = blocked_write
start_logging(sys.argv[1])
assert entered.wait(3)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.folder)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
