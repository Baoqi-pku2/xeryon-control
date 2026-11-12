"""Real Qt widgets tested without opening a CAN transport."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from xeryon_control.ui.main_window import MainWindow

_app = None


def create_console():
    global _app
    _app = QApplication.instance() or QApplication([])
    window = MainWindow()
    for timer in tuple(window._timers):
        window.cancel_timer(timer)
    return window
