"""Local form preferences. Only operator edits enter the persistent store."""

import os
from math import isfinite
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer

from xeryon_control.config import EDS_PATH


def preferences_path():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "XeryonControl" / "settings.ini"


# Keys are also the session's field names, without the _var suffix.
GROUPS = {
    "connection": ("interface", "channel", "bitrate", "node_id", "node_b_enabled",
                   "node_b_id", "encoder", "eds_path", "reset_on_connect", "boot_timeout", "probe_limit"),
    "motion": ("velocity", "acceleration", "max_velocity", "max_acceleration", "quick_stop_decel",
               "position_window", "position_window_time", "following_error_window", "following_error_timeout",
               "relative_distance", "jog_step", "home_direction", "home_offset", "home_timeout"),
    "workflow": ("workflow_start", "workflow_switch", "workflow_end", "workflow_fast_speed",
                 "workflow_slow_speed", "workflow_fast_acceleration", "workflow_travel_limit",
                 "workflow_timeout", "shortcut_dwell"),
    "axes": ("linked_mode", "invert_b", "pair_tolerance"),
    "limits": ("default_limit", "limit_enabled", "min_limit", "max_limit"),
    "test": ("test_speeds", "test_distances", "test_return", "test_settle", "test_timeout",
             "repeat_cycles", "repeat_span", "repeat_speed"),
}


def decode(name, value, default):
    # INI booleans may be returned as strings after a process restart.
    if isinstance(default, bool):
        return {"true": True, "false": False}.get(str(value).lower())
    if not isinstance(value, str) or not value.strip():
        return None
    if name in ("interface", "channel", "eds_path"):
        return value
    if name == "encoder":
        return value if value in ("10MU", "1MU", "250NAN", "100NAN") else None
    if name == "home_direction":
        return value if value in ("Positive index", "Negative index") else None
    parts = value.replace(";", ",").split(",") if name in ("test_speeds", "test_distances") else [value]
    try:
        return value if all(isfinite(float(part)) for part in parts) else None
    except ValueError:
        return None


class Preferences:
    def __init__(self, session, path, parent, report_error):
        self.path = str(path)
        self.report_error = report_error
        self.pending = {}
        self.timer = QTimer(parent)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.flush)
        store = self._open()
        fields = [(f"{group}/{name}", name, getattr(session, name + "_var"))
                  for group, names in GROUPS.items() for name in names]
        panel = session.position_panel
        fields.extend((f"panel/{name}", name, getattr(panel, name))
                      for name in ("speed", "span", "center_coordinate", "end_margin"))
        for key, name, field in fields:
            default = field.get()
            if store.contains(key):
                value = decode(name, store.value(key), default)
                if value is not None:
                    field.set(value)
            field.subscribe_edits(lambda value, k=key, n=name, d=default: self._changed(k, n, d, value))
        if store.status() != QSettings.Status.NoError:
            self.report_error(f"配置读取异常：{self.path}")

    def _open(self):
        return QSettings(self.path, QSettings.Format.IniFormat)

    def _changed(self, key, name, default, value):
        if decode(name, value, default) is None:
            self.pending.pop(key, None)
            return
        # A one-file EXE extracts its bundled EDS to a different path on each launch.
        self.pending[key] = None if name == "eds_path" and value == EDS_PATH else value
        self.timer.start()

    def flush(self):
        self.timer.stop()
        if not self.pending:
            return
        store = self._open()
        for key, value in self.pending.items():
            if value is None:
                store.remove(key)
            else:
                store.setValue(key, value)
        store.sync()
        if store.status() == QSettings.Status.NoError:
            self.pending.clear()
        else:
            self.report_error(f"配置保存失败：{self.path}")
