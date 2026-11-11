"""Offline responsive Tk checks; no real CAN network can be created."""
import argparse
import json
import tkinter as tk
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
from xeryon_control.ui.main_window import XeryonControlGui


def check(output):
    result = {"hardware_connected": False, "layouts": [], "checks": []}
    errors = []
    with patch("xeryon_control.ui.main_window.setup_network", side_effect=AssertionError("Offline check attempted CAN")):
        app = XeryonControlGui()
        # This audit injects state explicitly; live polling belongs to the separate
        # event-loop responsiveness check and would keep update() processing timers.
        for timer in app.tk.call("after", "info"):
            app.after_cancel(timer)
        app.report_callback_exception = lambda *args: errors.append(str(args))
        try:
            panel = app.position_panel
            for width, height in ((1440, 900), (1320, 820), (1280, 760), (1100, 760)):
                app.geometry(f"{width}x{height}+0+0")
                app.update()
                for _ in range(3):
                    app.update_idletasks()
                cards = (panel.connection_card, panel.control_card, panel.visual_card, panel.shortcut_card)
                overflow = [c.heading.cget("text") for c in cards
                            if c.winfo_x() < 0 or c.winfo_x() + c.winfo_width() > panel.winfo_width() + 2]
                button_overflow = [n for n, b in panel.shortcut_buttons.items()
                                   if b.winfo_x() + b.winfo_width() > panel.shortcut_grid.winfo_width() + 2]
                stop = app.global_stop_button
                stop_visible = (stop.winfo_rooty() >= app.winfo_rooty() and
                                stop.winfo_rootx() + stop.winfo_width() <= app.winfo_rootx() + app.winfo_width())
                result["layouts"].append({"requested": [width, height],
                    "actual": [app.winfo_width(), app.winfo_height()], "stacked": panel._compact,
                    "card_overflow": overflow, "preset_overflow": button_overflow,
                    "global_stop_visible": stop_visible})
                assert not overflow and not button_overflow and stop_visible
                assert all(c.winfo_y() + c.winfo_height() <= panel.winfo_height() + 2 for c in cards)
                assert panel.control_card.content.winfo_height() >= panel.control_card.content.winfo_reqheight()
            # Typical long errors remain visible without any page scrolling.
            app.shortcut_status_var.set("快捷流程中止 · Node 33: Failed to confirm control mode PROFILE_POSITION")
            app.target_status_var.set("未连接设备 · 无法确认停止，请使用硬件停止")
            app.update()
            panel.shortcut_dwell_entry.focus_force()
            app.update()
            y = panel.shortcut_dwell_entry.winfo_rooty()
            assert app.winfo_rooty() <= y < app.winfo_rooty() + app.winfo_height()
            assert panel.control_card.content.winfo_height() >= panel.control_card.content.winfo_reqheight()
            result["checks"].append("Controls and long stop status fit without page scrolling")
            # Preview and selecting presets must not dispatch hardware commands.
            app._safe_start = Mock()
            panel.user_target.set("1200")
            panel.scale.set(1500)
            panel.shortcut_buttons[1].invoke()
            panel.shortcut_buttons[3].invoke()
            assert app.shortcut_selected == [1, 3]
            app._safe_start.assert_not_called()
            panel.clear_shortcut_button.invoke()
            assert not app.shortcut_selected
            result["checks"].append("Target preview, preset selection and clearing never dispatch motion")
            app.global_stop_button.invoke()
            assert app.test_stop_requested.is_set()
            result["checks"].append("Persistent STOP reaches the existing stop handler")
            assert not errors, errors
        finally:
            for timer in app.tk.call("after", "info"):
                app.after_cancel(timer)
            app.destroy()
    result["passed"] = True
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    check(parser.parse_args().output)
