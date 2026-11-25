# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows release package (stage-220).
# Run by packaging/build_release.ps1 from the build venv, after a normal
# (not editable) install of openscope and openscope-gui. One-folder build:
# the Qt DLLs stay separate files that a user can replace (LGPL-3.0).
# Windowed (no console). No UPX.
import os

from PyInstaller.utils.hooks import collect_data_files

NAME = "openscope-gui"
# build_release.ps1 copies the image, its manifest and SOURCE.txt into this
# folder next to the exe; update_wizard.bundled_firmware_dir() reads it.
FIRMWARE_DIR = "firmware"

a = Analysis(
    [os.path.join(SPECPATH, "launch_gui.py")],
    pathex=[],
    binaries=[],
    datas=collect_data_files("openscope_gui"),      # pictures/*.svg
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "matplotlib", "IPython",
              "mcp", "openscope.mcp_server", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
