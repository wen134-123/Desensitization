#!/usr/bin/env bash
# macOS 打包脚本：生成 dist/BidRedact.app + dist/BidRedact.dmg
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="BidRedact"
DISPLAY_NAME="文档脱敏工作台"
PYTHON="${PYTHON:-python3}"
VENV=".venv-build"

if [ ! -d "$VENV" ]; then
  "$PYTHON" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install -U pip wheel
python -m pip install -r requirements.txt -r requirements-build.txt

echo "==> PyInstaller 构建 .app"
pyinstaller --noconfirm --clean bidredact.spec

APP_PATH="dist/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
  echo "错误：未找到 ${APP_PATH}，PyInstaller 构建可能失败" >&2
  exit 1
fi

echo
echo "==> 生成 .dmg 安装包"
DMG_PATH="dist/${APP_NAME}.dmg"
STAGING_DIR="dist/.dmg-staging"
rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"

# .app 本体（复制，hdiutil srcfolder 不允许把 dmg 本身放在 srcfolder 里的源目录）
cp -R "$APP_PATH" "$STAGING_DIR/"

# /Applications 快捷软链——用户拖到这里就安装
ln -s /Applications "$STAGING_DIR/Applications"

# 简单的 .DS_Store：设置图标视图为 100x100 网格，背景白，保持默认样式即可
# （如果想加背景图/自定义位置，需要挂载 DMG 写 .DS_Store，那套流程在受限环境下容易失败；
# 这里先用 srcfolder 一次成型，后续签名/公证完成后再加美化）

# hdiutil create：srcfolder 方式直接生成只读 HFS+ DMG
# -volname        DMG 挂载后显示的卷名
# -srcfolder      要打包的目录（含 .app + Applications 软链）
# -ov             覆盖已存在的 DMG
# -format UDZO    压缩的只读 DMG（常见 150MB .app → ~80MB .dmg）
hdiutil create \
  -volname "$DISPLAY_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH" > /dev/null

rm -rf "$STAGING_DIR"

APP_SIZE=$(du -sh "$APP_PATH" | awk '{print $1}')
DMG_SIZE=$(du -sh "$DMG_PATH" | awk '{print $1}')

echo
echo "============================================="
echo "  构建完成"
echo "============================================="
echo "  App :  ${APP_PATH}  ($APP_SIZE)"
echo "  DMG :  ${DMG_PATH}  ($DMG_SIZE)"
echo
echo "本地试运行：open ${APP_PATH}"
echo "分发前建议签名与公证（需要 Apple Developer ID）："
echo '  codesign --deep --force --options runtime --sign "Developer ID Application: <NAME> (<TEAMID>)" dist/BidRedact.app'
echo '  ditto -c -k --keepParent dist/BidRedact.app dist/BidRedact.zip'
echo '  xcrun notarytool submit dist/BidRedact.zip --keychain-profile <PROFILE> --wait'
echo '  xcrun stapler staple dist/BidRedact.app'
echo
echo "签名后 DMG 需重新打包："
echo "  hdiutil create -volname '$DISPLAY_NAME' -srcfolder dist/BidRedact.app -ov -format UDZO dist/BidRedact.dmg"
