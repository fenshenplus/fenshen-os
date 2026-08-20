# -*- mode: python ; coding: utf-8 -*-

import sqlite3 as _sqlite3_mod
import os as _os

# 该托管 Python 把 _sqlite3 静态链进解释器，PyInstaller 会误判 sqlite3 为“完全内建”
# 而跳过整个包（含 __init__.py），导致冻结后 `import sqlite3` 报 No module named 'sqlite3'。
# 显式把 sqlite3 包目录打进 datas，运行时 __init__.py 的 `from _sqlite3 import *` 由内建解释器解析。
_SQLITE_DIR = _os.path.dirname(_sqlite3_mod.__file__)

a = Analysis(
    ['packaging/macos/run_app.py'],
    pathex=[_os.getcwd()],
    binaries=[],
    datas=[('backend', 'backend'), ('frontend', 'frontend'), ('packaging/macos/permission_guide.html', '.'), (_SQLITE_DIR, 'sqlite3')],
    hiddenimports=['uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.websockets', 'uvicorn.server', 'uvicorn.supervisors', 'multiprocessing', 'pkg_resources', 'webview', 'webview.platforms.cocoa', 'objc', 'Cocoa', 'WebKit'],
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
    name='分身',
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
)
