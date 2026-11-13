"""PySide6 desktop window and main-thread bridge for the controller session."""

import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from xeryon_control.application.session import ControllerSession, POLL_INTERVAL_MS
from xeryon_control.config import EDS_PATH
from xeryon_control.ui.advanced import AdvancedPages
from xeryon_control.ui.icons import icon
from xeryon_control.ui.position_panel import PositionPanel
from xeryon_control.ui.preferences import Preferences, preferences_path
from xeryon_control.ui.travel_readout import TravelReadout
from xeryon_control.ui.theme import apply_theme
from xeryon_control.ui.widgets import (
    button,
    divider,
    glyph,
    card,
    check,
    edit,
    label,
    row,
    scroll,
    value_label,
)


class MainWindow(QMainWindow):
    finished = Signal()

    def __init__(self, session=None, *, settings_file=None):
        super().__init__()
        self.session = s = session if session is not None else ControllerSession()
        self.preferences = None
        if settings_file is not None:
            self.preferences = Preferences(s, settings_file, self, lambda message: s._post("log", message))
        self.setWindowTitle("Xeryon 电极推进器控制台")
        self._allow_close = False
        self._timers = set()
        self.error_dialog = self.confirmation = self.close_notice = None
        self.setup_dialog = None
        self.session.view = self
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header, hl = card()
        header.setObjectName("header")
        hl.setContentsMargins(16, 5, 14, 5)
        self.stack = QStackedWidget()
        self.console_tab = button("控制台", lambda: self.select_page(0), "nav")
        self.advanced_tab = button("高级设置", lambda: self.select_page(1), "nav")
        for b in (self.console_tab, self.advanced_tab):
            b.setCheckable(True)
        self.console_tab.setIcon(icon("console"))
        self.advanced_tab.setIcon(icon("settings"))
        self.connection = label(role="connection")
        self.controller_state = label()
        self.global_stop = button("停止   Esc", s.stop_actuator_test, "stop")
        self.global_stop.setToolTip("STOP：停止运动并关闭驱动（Esc）")
        self.global_stop.setIcon(icon("stop", "#ffffff"))
        hl.addLayout(
            row(
                glyph("brand", 30),
                label("Xeryon Control", "brand"),
                divider(True),
                self.console_tab,
                self.advanced_tab,
                None,
                self.connection,
                divider(True),
                self.controller_state,
                divider(True),
                value_label(s.active_axis_var, "muted"),
                self.global_stop,
            )
        )
        layout.addWidget(header)
        self.panel = PositionPanel(s)
        self.stack.addWidget(scroll(self.panel))
        self.advanced = AdvancedPages(s, lambda: self.select_page(0), self.guarded)
        self.stack.addWidget(self.advanced)
        layout.addWidget(self.stack, 1)
        self.select_page(0)
        self.screen_area = (
            QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        ).availableGeometry()
        area = self.screen_area
        self.resize(
            min(1600, max(600, int(area.width() * 0.92)), area.width() - 16),
            min(1000, max(540, int(area.height() * 0.9)), area.height() - 48),
        )
        self.move(area.center() - self.frameGeometry().center() + self.pos())
        s._set_connected_state(False)
        QApplication.instance().installEventFilter(self)
        self.schedule(0, self.center_first_show)
        self.schedule(100, s._drain_ui_queue)
        self.schedule(POLL_INTERVAL_MS, s._poll_status)
        self.refresh()

    def center_first_show(self):
        if self.isVisible():
            g = self.frameGeometry()
            g.moveCenter(self.screen_area.center())
            self.move(g.topLeft())

    def schedule(self, delay_ms, callback):
        timer = QTimer(self)
        timer.setSingleShot(True)
        self._timers.add(timer)

        def fire():
            self._timers.discard(timer)
            timer.deleteLater()
            if not self._allow_close:
                callback()

        timer.timeout.connect(fire)
        timer.start(delay_ms)
        return timer

    def cancel_timer(self, timer):
        if timer in self._timers:
            self._timers.remove(timer)
            timer.stop()
            timer.deleteLater()

    def eventFilter(self, obj, event):
        if (
            event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and event.key() == Qt.Key.Key_Escape
            and isinstance(obj, QWidget)
        ):
            owner = obj
            while owner is not None:
                if owner is self:
                    event.accept()
                    if (
                        event.type() == QEvent.Type.KeyPress
                        and not event.isAutoRepeat()
                    ):
                        self.session.stop_actuator_test()
                    return True
                owner = owner.parentWidget()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.panel.reflow(7 if event.size().width() >= 1100 else 4)

    def select_page(self, i):
        self.stack.setCurrentIndex(i)
        self.console_tab.setChecked(i == 0)
        self.advanced_tab.setChecked(i == 1)

    def guarded(self, action):
        try:
            action()
        except (ValueError, RuntimeError, OSError) as exc:
            self.session._show_error(str(exc))

    def refresh(self):
        self.panel.refresh()
        self.advanced.refresh()
        s = self.session
        if self.setup_dialog is not None:
            self.setup_dialog.setEnabled(not s.position_panel.editing_locked())
        online = s.connected_var.get() == "Connected"
        if self.connection.property("online") != online:
            self.connection.setProperty("online", online)
            self.connection.style().unpolish(self.connection)
            self.connection.style().polish(self.connection)
        self.connection.setText(
            {"Connected": "● 已连接", "Disconnected": "○ 未连接"}.get(
                s.connected_var.get(), s.connected_var.get()
            )
        )
        self.controller_state.setText(
            "控制器 " + ("空闲" if s.busy_var.get() == "Idle" else s.busy_var.get())
        )

    def append_logs(self, messages):
        now = time.strftime("%H:%M:%S")
        self.advanced.log.appendPlainText(
            "\n".join(f"[{now}] {msg}" for msg in messages)
        )

    def clear_results(self):
        self.advanced.table.setRowCount(0)

    def append_result(self, payload):
        self.advanced.append_result(payload)

    def choose_csv(self, save):
        dialog = QFileDialog(self, "导出测试结果")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter("CSV (*.csv)")
        dialog.setDefaultSuffix("csv")
        dialog.selectFile("encoder_results.csv")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.fileSelected.connect(save)
        dialog.finished.connect(dialog.deleteLater)
        dialog.show()

    def confirm(self, title, text, on_accept):
        if getattr(self, "repeat_dialog", None) is not None:
            self.present(self.repeat_dialog)
            return
        win, box, _ = self.notice(title, text)
        self.repeat_dialog = win

        def accept():
            win.accept()
            self.guarded(on_accept)

        def finished():
            self.repeat_dialog = None
            win.deleteLater()

        win.finished.connect(finished)
        box.addLayout(
            row(button("确认", accept, "primary"), button("取消", win.reject))
        )
        self.present(win)

    def notice(self, title, text):
        win = QDialog(self)
        win.setWindowTitle(title)
        win.setModal(False)
        box = QVBoxLayout(win)
        box.setContentsMargins(18, 16, 18, 16)
        message = label(text)
        message.setWordWrap(True)
        box.addWidget(message)
        win.setMinimumWidth(min(460, self.screen().availableGeometry().width() - 32))
        return win, box, message

    def present(self, win):
        win.adjustSize()
        win.show()
        g = win.frameGeometry()
        g.moveCenter(self.frameGeometry().center())
        area = self.screen().availableGeometry()
        win.move(
            max(area.left(), min(g.left(), area.right() - g.width() + 1)),
            max(area.top(), min(g.top(), area.bottom() - g.height() + 1)),
        )
        win.raise_()

    def show_error(self, text):
        if self.error_dialog is None:
            win, box, self.error_text = self.notice("控制器提示", text)
            self.error_dialog = win
            box.addLayout(
                row(
                    button("停止 STOP", self.session.stop_actuator_test, "localStop"),
                    button("关闭提示", win.close),
                )
            )
        self.error_text.setText(text)
        self.present(self.error_dialog)

    def show_motion_setup(self):
        m = self.session.position_panel
        if m.editing_locked():
            return
        if self.setup_dialog is None:
            win, box, _ = self.notice(
                "行程检查",
                "核对中位和两端预留。仅本次连接有效。",
            )
            self.setup_dialog = win
            box.addWidget(TravelReadout(m.travel_readings))
            box.addLayout(row(label("机械中位 (µm)"), edit(m.center_coordinate)))
            box.addWidget(
                button("使用当前位置作为中位", m.use_current_position_as_center)
            )
            box.addLayout(row(label("每端预留 (µm)"), edit(m.end_margin)))
            box.addWidget(
                check(
                    "确认实物位于机械中位", m.center_checked, m._mapping_check_changed
                )
            )

            def apply():
                if m.apply_travel():
                    win.hide()

            box.addLayout(row(button("确认行程映射", apply), button("取消", win.hide)))
        self.present(self.setup_dialog)

    def show_confirmation(self, message, absolute, problem, review):
        self.close_confirmation()
        win, box, _ = self.notice("起点位置已变化", message)
        self.confirmation = win
        box.addWidget(label(absolute, "muted"), 0, Qt.AlignmentFlag.AlignRight)
        self.problem = label(problem)
        self.problem.setWordWrap(True)
        box.addWidget(self.problem)
        self.continue_button = button(
            "继续", lambda: self.session._continue_start(review), "primary"
        )
        self.cancel_button = button("取消", self.session._dismiss_start_confirmation)
        box.addLayout(row(None, self.continue_button, self.cancel_button))
        self.continue_button.setEnabled(not problem)
        win.rejected.connect(
            lambda: self.session._dismiss_start_confirmation()
            if self.confirmation is win
            else None
        )
        self.present(win)

    def block_confirmation(self, text):
        self.continue_button.setEnabled(False)
        self.problem.setText(text)
        self.present(self.confirmation)

    def close_confirmation(self):
        if self.confirmation is not None:
            win, self.confirmation = self.confirmation, None
            win.hide()
            win.deleteLater()

    def close_dialogs(self):
        if self.error_dialog:
            self.error_dialog.hide()
        if self.setup_dialog:
            self.setup_dialog.hide()
        if getattr(self, "repeat_dialog", None):
            self.repeat_dialog.reject()

    def show_close_notice(self):
        if self.close_notice is None:
            win, box, _ = self.notice(
                "等待控制器退出",
                "正在确认停止或断开连接。\n手动退出前，请确认所有轴已断电、推杆已停稳。",
            )
            self.close_notice = win
            box.addWidget(button("已确认断电并停止，退出软件", self.finish_exit))
            box.addWidget(button("隐藏提示", win.hide))
        self.present(self.close_notice)
        return self.close_notice

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
            self.session._on_close()

    def finish_exit(self):
        if self._allow_close:
            return
        self._allow_close = True
        if self.preferences is not None:
            self.preferences.flush()
        for timer in tuple(self._timers):
            self.cancel_timer(timer)
        QApplication.instance().removeEventFilter(self)
        self.close()
        self.finished.emit()


def create_desktop(*, settings_file=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "resources" / "xeryon-control.ico")))
    apply_theme(app)
    window = MainWindow(settings_file=settings_file)
    if window.preferences is not None:
        app.aboutToQuit.connect(window.preferences.flush)
    window.finished.connect(app.quit)
    return app, window


def main():
    app, window = create_desktop(settings_file=preferences_path())
    window.show()
    if "--smoke-test" in sys.argv:
        app.processEvents()
        assert Path(EDS_PATH).is_file()
        assert any(size.width() == 256 for size in window.windowIcon().availableSizes())
        assert window.session.network is None
        assert window.panel.actual.text() == "—"
        assert window.advanced.count() == 6 and len(window.panel.presets) == 7
        window.close()
        return
    sys.exit(app.exec())
