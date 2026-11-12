"""Offline Qt layout and control checks at workstation and laptop sizes."""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from xeryon_control.ui.main_window import create_desktop


def check(output, preview=None):
    result = {"hardware_connected": False, "layouts": []}
    with patch(
        "xeryon_control.application.session.setup_network",
        side_effect=AssertionError("unexpected CAN access"),
    ):
        qt, window = create_desktop()
        session = window.session
        try:
            window.show()
            qt.processEvents()
            for width, height in ((1440, 900), (1320, 820), (1100, 760), (900, 640)):
                window.resize(width, height)
                for _ in range(4):
                    qt.processEvents()
                panel = window.panel
                stop = window.global_stop
                top_left = stop.mapTo(window, QPoint(0, 0))
                assert window.rect().contains(top_left) and window.rect().contains(
                    top_left + QPoint(stop.width() - 1, stop.height() - 1)
                )
                assert len(panel.presets) == 7 and all(
                    b.isVisible() for b in panel.presets.values()
                )
                assert window.advanced.count() == 6
                area = window.stack.widget(0)
                assert window.width() == width, (width, window.width())
                assert panel.columns == (7 if width >= 1100 else 4)
                assert area.horizontalScrollBar().maximum() == 0
                for preset in panel.presets.values():
                    area.ensureWidgetVisible(preset)
                    qt.processEvents()
                    center = preset.mapTo(area.viewport(), preset.rect().center())
                    assert area.viewport().rect().contains(center)
                area.verticalScrollBar().setValue(0)
                assert (
                    area.verticalScrollBar().maximum()
                    >= panel.height() - area.viewport().height() - 1
                )
                result["layouts"].append(
                    {
                        "window": [window.width(), window.height()],
                        "columns": panel.columns,
                        "scroll_x": area.horizontalScrollBar().maximum(),
                        "scroll_y": area.verticalScrollBar().maximum(),
                    }
                )
            # Exercise the two combo variants through their real widgets.
            current_port = session.channel_var.get()
            panel.port.lineEdit().selectAll()
            QTest.keyClicks(panel.port.lineEdit(), "COM47")
            assert session.channel_var.get() == "COM47"
            session.channel_var.set(current_port)
            window.select_page(1)
            window.advanced.setCurrentIndex(1)
            encoder = window.advanced.encoder
            original_encoder = session.encoder_var.get()
            window.advanced.currentWidget().ensureWidgetVisible(encoder)
            window.activateWindow()
            encoder.setFocus()
            qt.processEvents()
            QTest.mouseClick(encoder, Qt.MouseButton.LeftButton,
                             pos=QPoint(encoder.width() - 12, encoder.height() // 2))
            assert QTest.qWaitForWindowExposed(encoder.view().window(), 2000)
            assert encoder.view().isVisible()
            QTest.keyClick(encoder.view(), Qt.Key.Key_End)
            QTest.keyClick(encoder.view(), Qt.Key.Key_Return)
            qt.processEvents()
            assert session.encoder_var.get() == encoder.itemText(encoder.count() - 1)
            assert not encoder.view().isVisible()
            session.encoder_var.set(original_encoder)
            window.select_page(0)
            session._safe_start = Mock()
            panel.slider.setValue(1500)
            panel.presets[1].click()
            panel.presets[3].click()
            assert session.shortcut_selected == [1, 3]
            session._safe_start.assert_not_called()
            panel.clear.click()
            assert not session.shortcut_selected
            window.global_stop.click()
            assert session.test_stop_requested.is_set()
            if preview:
                for timer in tuple(window._timers):
                    window.cancel_timer(timer)
                session.test_stop_requested.clear()
                session.channel_var.set("COM4")
                session._set_connected_state(True)
                session.node = Mock(id=32)
                session.network = Mock()
                session.nodes = {"Actuator A": session.node}
                panel.model.apply_user_zero(-37000, time.monotonic())
                session._set_status(
                    dict(
                        axis="Actuator A",
                        position_um=-34550,
                        position_inc=session._um_to_inc(-34550),
                        sample_time=time.monotonic(),
                        statusword=0x0650,
                        state="SWITCH ON DISABLED",
                        mode="PROFILE_POSITION",
                    )
                )
                panel.model.user_target.set("3350")
                panel.model.speed.set("300")
                panel.model.span.set("5000")
                panel.model.center_view()
                session.target_status_var.set("等待启动")
                session._update_shortcut_status()
                window.resize(1440, 850)
                window.refresh()
                qt.processEvents()
                assert window.grab().save(str(preview))
        finally:
            window.finish_exit()
    result["passed"] = True
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    check(args.output, args.preview)
