# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys
import sysconfig

project_root = Path(SPECPATH)
app_icon = project_root / "src/xeryon_control/resources/xeryon-control.ico"
# Qt imports the Windows ICU API. A same-named DLL from an unrelated PATH
# entry can export version-suffixed symbols and make the frozen QtCore fail.
runtime = Path(sys.base_prefix)
packages = Path(sysconfig.get_path("platlib"))
windows = Path(os.environ["SystemRoot"])
os.environ["PATH"] = os.pathsep.join(str(directory) for directory in (
    runtime, runtime / "DLLs", packages / "PySide6", packages / "shiboken6",
    windows / "System32", windows,
))

a = Analysis(
    [str(project_root / "src/xeryon_control/__main__.py")],
    pathex=[str(project_root / "src")],
    datas=[
        (str(project_root / "docs/NOTICE.txt"), "docs"),
        (str(project_root / "docs/reference/xeryon_xla_5_eds_docu.txt"), "docs/reference"),
        (str(app_icon), "xeryon_control/resources"),
        (str(project_root / "src/xeryon_control/resources/eds"), "xeryon_control/resources/eds"),
    ],
    hiddenimports=["can.interfaces.slcan"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="XeryonControl",
    icon=str(app_icon),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
