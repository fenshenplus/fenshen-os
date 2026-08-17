# -*- mode: python ; coding: utf-8 -*-
"""分身 v5.4 PyInstaller spec（Windows exe）
用法（在 Windows 上）:  pyinstaller --clean packaging/windows/fenshen.spec
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

datas = [
    (os.path.join(ROOT, "frontend"), "frontend"),
    (os.path.join(ROOT, "backend"), "backend"),
]

hiddenimports = [
    "sqlite3",
    "requests",
    "paramiko",
    "fastapi",
    "fastapi.responses",
    "fastapi.staticfiles",
    "fastapi.middleware",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

a = Analysis(
    [os.path.join(SPECPATH, "run.py")],
    pathex=[ROOT, os.path.join(ROOT, "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "site"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="分身",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
