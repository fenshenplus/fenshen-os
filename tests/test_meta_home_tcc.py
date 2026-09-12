"""元神家路径 · macOS TCC 受保护目录检测（v0.75.4）· 确定性单测。

背景：分身常驻是 launchd LaunchAgent 后台拉起的。macOS TCC 把「桌面/文稿/下载」等
列为受保护目录，守护进程拿不到权限时**会直接卡死不启动**（2026-09-12 实测：
home=~/Desktop/元神 → launchd 启动后永不监听 8002；前台手动跑却正常；
改到 ~/元神 后 10 秒就绪）。默认家目录因此从 ~/Desktop/元神 改为 ~/元神，
并在 /api/meta/home 暴露 tcc_risk 供前端提示。

运行：python -m pytest tests/test_meta_home_tcc.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FENSHEN_DB_PATH",
                      os.path.join(tempfile.gettempdir(), "fenshen_tcc_test.db"))

from backend import main as M  # noqa: E402

HOME = os.path.abspath(os.path.expanduser("~"))


def test_protected_dirs_flagged():
    for name in ("Desktop", "Documents", "Downloads"):
        assert M._home_under_tcc_protected(os.path.join(HOME, name, "元神")) is True, \
            f"~/ {name} 属于 TCC 受保护目录，必须标记风险"


def test_safe_paths_not_flagged():
    for p in (os.path.join(HOME, "元神"), os.path.join(HOME, ".fenshen", "home"),
              os.path.join(tempfile.gettempdir(), "fenshen_mh3")):
        assert M._home_under_tcc_protected(p) is False, f"{p} 不应被标记"


def test_default_home_is_not_protected():
    """默认家目录绝不能落在 TCC 受保护目录下，否则守护起不来。"""
    default = os.path.expanduser("~/元神")
    assert M._home_under_tcc_protected(default) is False
    # 历史默认值（桌面）已被证明会导致守护卡死，锁死不再回退
    assert M._home_under_tcc_protected(os.path.expanduser("~/Desktop/元神")) is True


def test_relative_and_tilde_supported():
    assert M._home_under_tcc_protected("~/Desktop/元神") is True
    assert M._home_under_tcc_protected("~/元神") is False
