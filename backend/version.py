"""
分身版本单一真源（Single Source of Truth）。

所有版本号、release 标识、schema 版本、构建日期、git commit 都从这里读取，
禁止在 main.py / frontend / 测试里硬编码版本字符串（改版本只改这一处）。

版本规则（语义化，详见 VERSION_MANAGEMENT.md）：
- MAJOR：不兼容的 API / 数据契约变更
- MINOR：向后兼容的功能新增
- PATCH：向后兼容的问题修复

SEMVER 与 RELEASE 解耦：内部 semver 连续递增，RELEASE 是对外营销 / 签字版本。
"""
import os
import subprocess

# ── 版本号（语义化 MAJOR.MINOR.PATCH）──
SEMVER = "0.64.51"

# ── 发布标识（对外给人看的版本，如 v6.4）──
RELEASE = "v6.4"

# ── 数据库 schema 迁移版本（init_db 据此决定跑哪些迁移）──
SCHEMA_VERSION = 1

# ── 构建日期（发版时更新；scripts/bump_version.py 会自动维护）──
BUILD_DATE = "2026-09-04"


def _git_commit() -> str:
    """读取当前 git commit 短哈希；非 git 环境 / 打包环境回退 unknown。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


COMMIT = _git_commit()


def as_dict() -> dict:
    """结构化版本信息，供 /api/version 与 /api/health 复用。"""
    return {
        "semver": SEMVER,
        "release": RELEASE,
        "schema_version": SCHEMA_VERSION,
        "build_date": BUILD_DATE,
        "git_commit": COMMIT,
    }
