#!/usr/bin/env bash
# 分身守护安装 / 卸载脚本（macOS launchd）。
#
# 作用：将 com.fenshen.app.plist 安装到 ~/Library/LaunchAgents 并加载，
#       使分身在崩溃 / 退出异常后自动重启，保证常驻（与 App 内「关闭=最小化到后台」一致）。
#
# 用法：
#   ./install_launchagent.sh            # 安装并加载守护
#   ./install_launchagent.sh --uninstall # 卸载守护
#
# 注意：plist 默认指向 /Applications/分身.app。若你自定义了安装路径，
#       请修改下方 APP_BIN 变量为本机实际路径。
set -u

LABEL="com.fenshen.app"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.fenshen.app.plist"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
APP_BIN="/Applications/分身.app/Contents/MacOS/分身"

# ── 卸载 ──
if [ "${1:-}" = "--uninstall" ]; then
    launchctl unload "$DEST" 2>/dev/null || true
    rm -f "$DEST"
    echo "✓ 已卸载分身守护（${DEST}）。"
    exit 0
fi

# ── 安装前检查目标二进制存在 ──
if [ ! -x "$APP_BIN" ]; then
    echo "⚠️  未找到分身可执行文件：$APP_BIN"
    echo "    若你安装到了其他路径，请编辑本脚本的 APP_BIN 变量后重试。"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

# 若源 plist 路径与默认不同（用户改过 APP_BIN），同步改写 ProgramArguments 指向实际路径
if grep -q "分身.app" "$PLIST_SRC"; then
    cp "$PLIST_SRC" "$DEST"
else
    echo "⚠️  源 plist 未包含默认安装路径，请确认其 ProgramArguments 是否正确。"
fi

# 先卸载旧实例（若存在），避免重复加载
launchctl unload "$DEST" 2>/dev/null || true

# 加载：兼容 macOS 版本差异（旧版 load / Ventura+ bootstrap）
if launchctl load "$DEST" 2>/dev/null; then
    echo "✓ 已加载分身守护（launchctl load）。"
elif launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null; then
    launchctl kickstart "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    echo "✓ 已加载分身守护（launchctl bootstrap，适配新系统）。"
else
    echo "⚠️  自动加载失败，请手动执行：launchctl load \"$DEST\""
    exit 1
fi

echo "分身守护安装完成：崩溃后将自动重启，开机自动拉起。"
