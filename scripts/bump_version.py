#!/usr/bin/env python3
"""
分身版本质量门禁 + 发版助手（单一真源 backend/version.py）。

门禁（fail-closed，任一失败即中止，绝不 bump）：
  1. 语法编译后端 + 测试（快速前置）
  2. 回归冒烟 tests/smoke_v40.py（默认命中本机 8002 运行实例，须全绿）

通过后才改写：
  - backend/version.py 的 SEMVER / RELEASE / BUILD_DATE
  - CHANGELOG.md 顶部 [Unreleased] 下生成新版本段骨架
  - VERSION_MANAGEMENT.md 顶部「当前版本 / 最后更新」行

用法：
  python scripts/bump_version.py minor --release v6.5 --message "蒸馏体验打磨"
  python scripts/bump_version.py patch
  python scripts/bump_version.py major --skip-tests   # 紧急逃生舱，慎用

约定：SEMVER 连续递增（0.64.0→0.65.0）；RELEASE 是对外营销/签字版本（v6.4），
二者解耦——不加 --release 时 RELEASE 保持不变。
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(ROOT, "backend", "version.py")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
VMGMT = os.path.join(ROOT, "VERSION_MANAGEMENT.md")
SMOKE = os.path.join(ROOT, "tests", "smoke_v40.py")


def read_semver() -> str:
    m = re.search(r'SEMVER\s*=\s*"([\d.]+)"', open(VERSION_FILE).read())
    return m.group(1) if m else "0.0.0"


def bump(semver: str, kind: str) -> str:
    major, minor, patch = (int(x) for x in semver.split("."))
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def compile_gate() -> bool:
    print("▶ 语法编译门禁 (backend + tests)…")
    r = subprocess.run([sys.executable, "-m", "py_compile",
                        os.path.join(ROOT, "backend", "main.py"),
                        os.path.join(ROOT, "backend", "version.py"),
                        os.path.join(ROOT, "backend", "meta_distill.py"),
                        SMOKE])
    return r.returncode == 0


def smoke_gate() -> bool:
    base = os.environ.get("FENSHEN_SMOKE_BASE", "http://127.0.0.1:8002")
    print(f"▶ 回归冒烟门禁 (tests/smoke_v40.py → {base})…")
    print("  （需目标引擎运行中；冒烟用例自带清理，不污染业务数据）")
    r = subprocess.run([sys.executable, SMOKE, "--base", base])
    return r.returncode == 0


def write_version(new_semver: str, release: str, date: str) -> None:
    src = open(VERSION_FILE).read()
    src = re.sub(r'SEMVER\s*=\s*"\d+\.\d+\.\d+"', f'SEMVER = "{new_semver}"', src)
    if release:
        src = re.sub(r'RELEASE\s*=\s*"v[\d.]+"', f'RELEASE = "{release}"', src)
    src = re.sub(r'BUILD_DATE\s*=\s*"\d{4}-\d{2}-\d{2}"', f'BUILD_DATE = "{date}"', src)
    open(VERSION_FILE, "w").write(src)


def write_changelog(new_semver: str, release: str, date: str, msg: str) -> None:
    heading = release or f"v{new_semver}"
    entry = (f"\n## [{heading}] — {date}\n"
             f"### Added\n- {msg or '（待补条目）'}\n")
    c = open(CHANGELOG).read()
    if "## [Unreleased]" in c:
        c = c.replace("## [Unreleased]\n", "## [Unreleased]\n" + entry, 1)
    else:
        c = c.replace("# Changelog\n", "# Changelog\n" + entry, 1)
    open(CHANGELOG, "w").write(c)


def write_vmgmt(release: str, date: str) -> None:
    if not os.path.exists(VMGMT):
        return
    c = open(VMGMT).read()
    c = re.sub(r"当前版本：\*\*v[\d.]+\*\*", f"当前版本：**{release}**", c)
    c = re.sub(r"最后更新：\d{4}-\d{2}-\d{2}", f"最后更新：{date}", c)
    open(VMGMT, "w").write(c)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["major", "minor", "patch"])
    ap.add_argument("--release", help="对外 release 标识，如 v6.5（不加则 RELEASE 不变）")
    ap.add_argument("--message", help="CHANGELOG 新版本段摘要")
    ap.add_argument("--skip-tests", action="store_true", help="紧急逃生舱：跳过测试门禁（慎用）")
    args = ap.parse_args()

    cur = read_semver()
    new = bump(cur, args.kind)
    release = args.release or ""
    date = datetime.date.today().isoformat()

    print(f"当前 SEMVER={cur} → 目标 {new}" + (f" (release {release})" if release else " (RELEASE 不变)"))

    if not args.skip_tests:
        if not compile_gate():
            sys.exit("✗ 语法门禁失败，已中止 bump。")
        if not smoke_gate():
            sys.exit("✗ 回归冒烟未全绿，已中止 bump（fail-closed）。")

    write_version(new, release, date)
    write_changelog(new, release, date, args.message)
    write_vmgmt(release or _current_release(), date)
    print(f"✓ 已 bump 至 {new}" + (f" / {release}" if release else "") + "，并更新 CHANGELOG.md + VERSION_MANAGEMENT.md")
    print("  下一步：git commit + git tag + 跑安装包/官网推送。")


def _current_release() -> str:
    m = re.search(r'RELEASE\s*=\s*"(v[\d.]+)"', open(VERSION_FILE).read())
    return m.group(1) if m else "v?"


if __name__ == "__main__":
    main()
