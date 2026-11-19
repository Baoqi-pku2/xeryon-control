"""Bounded, asynchronous runtime logs and motion traces; no Qt or device I/O."""
from __future__ import annotations

import atexit
from collections import defaultdict, deque
import copy
from datetime import datetime
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time


def logs_folder() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XeryonControl" / "logs"


class _RotatingSink:
    """Writer-owned file; not registered in logging.shutdown's blocking handlers."""

    def __init__(self, path, *, max_bytes, backups):
        self.path, self.max_bytes, self.backups = path, max_bytes, backups
        self.stream = None
        self.formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")
        self._open()

    def _open(self):
        self.stream = self.path.open("a", encoding="utf-8", newline="\n")
        self.size = self.stream.tell()

    def _rotate(self):
        self.close()
        if self.backups:
            self.path.with_name(f"{self.path.name}.{self.backups}").unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                if source.exists():
                    source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._open()

    def emit(self, record):
        line = self.formatter.format(record) + "\n"
        length = len(line.encode("utf-8"))
        if self.stream is None:
            self._open()
        if self.max_bytes > 0 and self.size and self.size + length > self.max_bytes:
            self._rotate()
        self.stream.write(line)
        self.stream.flush()
        self.size += length

    def close(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            stream.close()


class _QueueHandler(logging.Handler):
    def __init__(self, service):
        super().__init__(logging.INFO)
        self.service = service

    def emit(self, record):
        snapshot = copy.copy(record)
        snapshot.msg, snapshot.args = record.getMessage(), None
        self.service.submit(snapshot)


class LoggingService:
    def __init__(self, folder=None, *, capacity=8192, max_bytes=5 * 1024 * 1024,
                 backups=3, keep_sessions=20):
        self.folder = Path(folder) if folder is not None else logs_folder()
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + f"_{os.getpid()}"
        self.path = self.folder / self.session_id / "runtime.log"
        self.queue = queue.Queue(maxsize=capacity)
        self.handler = _QueueHandler(self)
        self.stopping = threading.Event()
        self.dropped = 0
        self._state_lock = threading.Lock()
        self._notices = deque(maxlen=16)
        self._last_failure = None
        self._max_bytes, self._backups = max_bytes, backups
        self._keep_sessions = keep_sessions
        self.thread = threading.Thread(target=self._run, name="log-writer", daemon=True)
        self.thread.start()

    def submit(self, item):
        # Never wait for disk space or the writer, including on the STOP lane.
        with self._state_lock:
            if self.stopping.is_set():
                return False
            try:
                self.queue.put_nowait(item)
                return True
            except queue.Full:
                if not self.dropped:
                    self._notices.append("日志写入积压，部分记录未保存。")
                self.dropped += 1
                return False

    def trace(self, path, line):
        return self.submit((Path(path), line))

    def take_notices(self):
        with self._state_lock:
            notices = list(self._notices)
            self._notices.clear()
        return notices

    def _notice(self, message):
        with self._state_lock:
            self._notices.append(message)
        if sys.stderr is not None:
            try:
                print(message, file=sys.stderr)
            except (OSError, ValueError):
                pass

    def flush(self, timeout=2.0):
        """Barrier for tests/exports only; never called by device commands."""
        done = threading.Event()
        return self.submit(done) and done.wait(timeout)

    def close(self, timeout=2.0):
        with self._state_lock:
            self.stopping.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _prune(self):
        # Only remove our runtime files. Leave result/trace files untouched.
        sessions = sorted((p for p in self.folder.iterdir()
                           if p.is_dir() and p.name[:8].isdigit()), reverse=True)
        for directory in sessions[self._keep_sessions:]:
            if directory == self.path.parent:
                continue
            try:
                for path in directory.glob("runtime.log*"):
                    path.unlink()
                directory.rmdir()
            except OSError:
                # Another process may still have the files open on Windows.
                continue

    def _write_traces(self, traces, sink):
        for path, lines in traces.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.writelines(lines)
            except OSError as exc:
                message = f"运动轨迹写入失败：{path}：{exc}"
                if self._failure(message) and sink is not None:
                    try:
                        sink.emit(logging.LogRecord(__name__, logging.ERROR, __file__, 0,
                                                    message, (), (type(exc), exc, exc.__traceback__)))
                    except Exception as log_exc:
                        self._failure(f"运行日志写入失败：{log_exc}")
        traces.clear()

    def _failure(self, message):
        if message != self._last_failure:
            self._last_failure = message
            self._notice(message)
            return True
        return False

    def _run(self):
        sink = None
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    first = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                batch = [first]
                # A short window batches position samples without delaying producers.
                deadline = time.monotonic() + 0.2
                while len(batch) < 256 and not isinstance(batch[-1], threading.Event):
                    try:
                        if self.stopping.is_set():
                            item = self.queue.get_nowait()
                        else:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            item = self.queue.get(timeout=remaining)
                        batch.append(item)
                    except queue.Empty:
                        break
                if sink is None:
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        sink = _RotatingSink(self.path, max_bytes=self._max_bytes,
                                             backups=self._backups)
                        self._prune()
                    except OSError as exc:
                        self._failure(f"运行日志不可写：{exc}")
                with self._state_lock:
                    dropped, self.dropped = self.dropped, 0
                if dropped:
                    message = f"日志队列已满，丢弃 {dropped} 条记录；本次记录不完整。"
                    self._notice(message)
                    batch.insert(0, logging.LogRecord(__name__, logging.WARNING, __file__, 0, message, (), None))
                traces = defaultdict(list)
                for item in batch:
                    if isinstance(item, threading.Event):
                        self._write_traces(traces, sink)
                        item.set()
                    elif isinstance(item, tuple):
                        traces[item[0]].append(item[1])
                    elif sink is not None:
                        try:
                            sink.emit(item)
                        except Exception as exc:
                            self._failure(f"运行日志写入失败：{exc}")
                self._write_traces(traces, sink)
        finally:
            if sink is not None:
                sink.close()


_service = None
_lifecycle_lock = threading.RLock()
_previous_hooks = None
_previous_levels = None


def current_logging():
    return _service


def start_logging(folder=None, *, install_hooks=True, **options):
    global _service, _previous_hooks, _previous_levels
    with _lifecycle_lock:
        if _service is not None:
            return _service
        service = LoggingService(folder, **options)
        root = logging.getLogger()
        _previous_levels = (root.level, logging.getLogger("canopen").level)
        root.setLevel(logging.INFO)
        logging.getLogger("canopen").setLevel(logging.WARNING)
        root.addHandler(service.handler)
        _service = service
        if install_hooks:
            _previous_hooks = (sys.excepthook, threading.excepthook)
            sys.excepthook = _exception_hook
            threading.excepthook = _thread_exception_hook
        logging.getLogger(__name__).info("Application started; session=%s; python=%s; frozen=%s",
                                        service.session_id, sys.version.split()[0], bool(getattr(sys, "frozen", False)))
        return service


def _exception_hook(exc_type, value, traceback):
    logging.getLogger("xeryon_control.exception").critical("Unhandled exception", exc_info=(exc_type, value, traceback))
    # Exceptions from Qt callbacks can return to the event loop: do not block it.
    if _service is not None:
        _service._notice(f"程序异常：{value}。详细信息见运行日志。")
    if _previous_hooks is not None:
        _previous_hooks[0](exc_type, value, traceback)


def _thread_exception_hook(args):
    logging.getLogger("xeryon_control.exception").critical(
        "Unhandled thread exception: %s", args.thread.name if args.thread else "unknown",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    if _service is not None:
        _service._notice(f"后台线程异常：{args.exc_value}。详细信息见运行日志。")
    if _previous_hooks is not None:
        _previous_hooks[1](args)


def shutdown_logging():
    global _service, _previous_hooks, _previous_levels
    with _lifecycle_lock:
        service = _service
        if service is None:
            return
        logging.getLogger(__name__).info("Application logging stopped")
        root = logging.getLogger()
        root.removeHandler(service.handler)
        if _previous_levels is not None:
            root.setLevel(_previous_levels[0])
            logging.getLogger("canopen").setLevel(_previous_levels[1])
            _previous_levels = None
        if _previous_hooks is not None:
            if sys.excepthook is _exception_hook:
                sys.excepthook = _previous_hooks[0]
            if threading.excepthook is _thread_exception_hook:
                threading.excepthook = _previous_hooks[1]
            _previous_hooks = None
        _service = None
        if not service.close():
            service._notice("日志退出等待超时，末尾记录可能未保存。")


atexit.register(shutdown_logging)
