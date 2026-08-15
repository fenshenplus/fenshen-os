#!/usr/bin/env bash
# 分身 macOS 签名 + 公证 一键脚本（v6.2）
# 前置（需用户自行购买/配置）：
#   1. Apple Developer Program 账号（$99/年，公司版需邓白氏 D-U-N-S）
#   2. "Developer ID Application" 证书（钥匙串中，名称形如 "Developer ID Application: 安徽叒叕创业投资有限公司 (TEAMID)"）
#   3. App Store Connect 专用密码（appleid.apple.com → 生成 App 专用密码）
#   4. 团队 ID（Developer Account 页可见）
#
# 用法：
#   APPLE_DEV_ID="Developer ID Application: 安徽叒叕创业投资有限公司 (XXXXXXXXXX)" \
#   APPLE_ID="your@email.com" \
#   APPLE_APP_PASSWORD="abcd-efgh-ijkl-mnop" \
#   APPLE_TEAM_ID="XXXXXXXXXX" \
#   ./scripts/macos-sign-notarize.sh dist/分身.app 分身.dmg
set -euo pipefail

APP_BUNDLE="${1:?用法: $0 <分身.app 路径> <输出.dmg 路径>}"
DMG_PATH="${2:?用法: $0 <分身.app 路径> <输出.dmg 路径>}"

APPLE_DEV_ID="${APPLE_DEV_ID:?请设置 APPLE_DEV_ID（Developer ID Application 证书名称）}"
APPLE_ID="${APPLE_ID:?请设置 APPLE_ID（Apple ID 邮箱）}"
APPLE_APP_PASSWORD="${APPLE_APP_PASSWORD:?请设置 APPLE_APP_PASSWORD（App 专用密码）}"
APPLE_TEAM_ID="${APPLE_TEAM_ID:?请设置 APPLE_TEAM_ID（团队 ID）}"

echo "==> [1/5] 深度签名 .app（启用 hardened runtime + 时间戳）"
codesign --deep --force --options runtime --timestamp \
  --sign "$APPLE_DEV_ID" "$APP_BUNDLE"

echo "==> [2/5] 校验签名"
codesign --verify --verbose "$APP_BUNDLE"

echo "==> [3/5] 制作 DMG（若已存在则覆盖）"
test -f "$DMG_PATH" && rm -f "$DMG_PATH"
# 若 dist 目录含 .app，以 dist 为源制作 dmg
SRC_DIR="$(dirname "$APP_BUNDLE")"
hdiutil create -volname "分身" -srcfolder "$SRC_DIR" -ov -format UDZO "$DMG_PATH"

echo "==> [4/5] 提交公证（notarytool，自动等待）"
xcrun notarytool submit "$DMG_PATH" \
  --apple-id "$APPLE_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --team-id "$APPLE_TEAM_ID" \
  --wait

echo "==> [5/5] 装订公证票据（stapler）"
xcrun stapler staple "$DMG_PATH"

echo "✅ 完成：DMG 已签名并公证 -> $DMG_PATH"
