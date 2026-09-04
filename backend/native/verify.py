"""分身原生标准库 · 验收与质量门模块（verify）。

提供「可确定性判定」的常见验收原语，供 Goal-Mode 的 judge 直接调用，不依赖 LLM。
返回 dict: {ok: bool, msg: str}。所有原语纯标准库（os/subprocess/urllib/sqlite3），离线可跑、可单测。

设计原则：质量门能确定性判定的，绝不烧 LLM —— 这是「原生基础能力强大」在验收环节的体现。
"""
import os
import subprocess
import urllib.request
from sqlite3 import Connection

# 允许命令类质量门执行的目录白名单前缀（沙箱护栏：禁止写/扫系统目录）
_ALLOWED_CWD_PREFIXES = (
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),  # 分身仓库根
    os.path.expanduser("~/WorkBuddy"),
    os.path.expanduser("~/Desktop"),
)


def _cwd_allowed(cwd: str) -> bool:
    if not cwd:
        return True
    ap = os.path.abspath(cwd)
    return any(ap == p or ap.startswith(p + os.sep) for p in _ALLOWED_CWD_PREFIXES)


def file_exists(conn: Connection | None, path: str) -> dict:
    """判定路径（文件或目录）是否存在。"""
    try:
        ok = os.path.exists(path)
        return {"ok": ok, "msg": f"存在: {path}" if ok else f"缺失: {path}"}
    except Exception as e:
        return {"ok": False, "msg": f"校验异常: {e}"}


def test_passed(conn: Connection | None, cmd: str, cwd: str = None, timeout: int = 120) -> dict:
    """运行命令（如 pytest），退出码 0 视为通过。cwd 限制在工作区白名单内。"""
    if cwd and not _cwd_allowed(cwd):
        return {"ok": False, "msg": f"cwd 越权被拒: {cwd}"}
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
        detail = "" if ok else (r.stderr or r.stdout)[:300]
        return {"ok": ok, "msg": f"退出码 {r.returncode}" + (f" | {detail}" if detail else "")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": f"命令超时({timeout}s): {cmd}"}
    except Exception as e:
        return {"ok": False, "msg": f"执行异常: {e}"}


def page_reachable(conn: Connection | None, url: str, expect_status: int = 200, timeout: int = 10) -> dict:
    """HTTP 探测页面可达（默认 200）。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status == expect_status
            return {"ok": ok, "msg": f"HTTP {resp.status}" + ("" if ok else f" (期望 {expect_status})")}
    except Exception as e:
        return {"ok": False, "msg": f"不可达: {url} ({e})"}


def db_row_count(conn: Connection, table: str, expect_ge: int = 1) -> dict:
    """判定表中行数 >= expect_ge（需传 conn）。"""
    if conn is None:
        return {"ok": False, "msg": "db_row_count 需要数据库连接"}
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ok = n >= expect_ge
        return {"ok": ok, "msg": f"{table} 行数 {n} (期望 ≥ {expect_ge})"}
    except Exception as e:
        return {"ok": False, "msg": f"查询异常: {e}"}


def build_ok(conn: Connection | None, project_root: str, timeout: int = 300) -> dict:
    """对前端/后端项目执行构建（按 marker 探测命令），无错即通过。"""
    if not _cwd_allowed(project_root):
        return {"ok": False, "msg": f"项目根越权被拒: {project_root}"}
    candidates = [
        ("package.json", "npm run build"),
        ("pnpm-lock.yaml", "pnpm build"),
        ("pyproject.toml", "python -m build"),
        ("setup.py", "python setup.py build"),
    ]
    for marker, cmd in candidates:
        if os.path.exists(os.path.join(project_root, marker)):
            r = subprocess.run(cmd, shell=True, cwd=project_root, capture_output=True, text=True, timeout=timeout)
            ok = r.returncode == 0
            detail = "" if ok else (r.stderr or r.stdout)[:300]
            return {"ok": ok, "msg": f"{cmd} 退出码 {r.returncode}" + (f" | {detail}" if detail else "")}
    return {"ok": False, "msg": f"未识别构建系统: {project_root}"}


# 登记进 NATIVE_CAPABILITIES 的 verify 子能力
VERIFY_CAPS = [
    {"name": "文件存在", "tool": "file_exists", "pure_stdlib": True},
    {"name": "测试通过", "tool": "test_passed", "pure_stdlib": True},
    {"name": "页面可达", "tool": "page_reachable", "pure_stdlib": True},
    {"name": "构建无误", "tool": "build_ok", "pure_stdlib": True},
    {"name": "数据行数", "tool": "db_row_count", "pure_stdlib": True},
]

__all__ = [
    "file_exists", "test_passed", "page_reachable", "db_row_count", "build_ok",
    "VERIFY_CAPS", "_cwd_allowed",
]
