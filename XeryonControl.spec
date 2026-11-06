# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

project_root = Path(SPECPATH)
conda_bin = Path(sys.base_prefix) / "Library" / "bin"
conda_dlls = [(str(path), ".") for path in conda_bin.glob("*.dll")] if conda_bin.exists() else []

a = Analysis(
    [str(project_root / "examples" / "control_gui.py")],
    pathex=[str(project_root / "examples")],
    binaries=conda_dlls,
    datas=[
        (str(project_root / "eds" / "xeryon_xla_5_eds.eds"), "eds"),
        (str(project_root / "eds" / "xeryon_xla_5_eds_docu.txt"), "eds"),
    ],
    hiddenimports=["can.interfaces.slcan"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
