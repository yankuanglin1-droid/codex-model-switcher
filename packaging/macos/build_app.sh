#!/usr/bin/env bash
# 把图形界面打包成可双击的 macOS 应用。
#
#   bash packaging/macos/build_app.sh                    装到 ~/Applications
#   bash packaging/macos/build_app.sh /Applications      装到指定目录
#
# 直接用原生 bundle 结构，不用 AppleScript 包装：
#   Contents/Info.plist     应用元信息（含版本号）
#   Contents/MacOS/launcher 入口脚本，转发给 packaging/macos/launch.sh
#
# 为什么不用 osacompile：AppleScript 里的 `do shell script` 会等前台命令结束，
# 后台服务跑起来之后双击图标会一直卡住，而且弹窗、权限都会引入额外变数。
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
APP_NAME="${CODEX_SWITCHER_APP_NAME:-Codex 多模型切换器}"
OUT_DIR="${1:-$HOME/Applications}"
APP_PATH="$OUT_DIR/$APP_NAME.app"
APP_HOME="${CODEX_SWITCHER_HOME:-$HOME/.local/share/codex-switcher}"
LAUNCHER="$APP_HOME/packaging/macos/launch.sh"
TOOL_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_ROOT/codex_switcher/__init__.py" | head -1)
[ -n "$TOOL_VERSION" ] || TOOL_VERSION="0.0.0"

# .app 必须指向非受保护目录里的运行时：~/Documents 下的脚本，从访达启动时会报
# “Operation not permitted”。所以先确保运行时已经安装好。
if [ ! -f "$LAUNCHER" ]; then
  echo "运行时还没安装，先执行 install.sh …"
  bash "$REPO_ROOT/install.sh" --no-init
fi
if [ ! -f "$LAUNCHER" ]; then
  echo "找不到 $LAUNCHER，请先运行 bash install.sh" >&2
  exit 1
fi
chmod +x "$LAUNCHER"

mkdir -p "$OUT_DIR"

case "$APP_PATH" in
  *.app) ;;
  *) echo "输出路径必须以 .app 结尾：$APP_PATH" >&2; exit 1 ;;
esac
if [ -e "$APP_PATH" ]; then
  echo "覆盖已存在的应用：$APP_PATH"
  rm -rf "$APP_PATH"
fi

mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"

cat > "$APP_PATH/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleIdentifier</key>
	<string>com.codex.model-switcher.gui</string>
	<key>CFBundleExecutable</key>
	<string>launcher</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>$TOOL_VERSION</string>
	<key>CFBundleVersion</key>
	<string>$TOOL_VERSION</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
</dict>
</plist>
PLIST

cat > "$APP_PATH/Contents/MacOS/launcher" <<ENTRY
#!/bin/sh
# 由 build_app.sh 生成：转发给仓库里的启动脚本。
# 目标路径在构建时写死；仓库搬家后重新执行一次 build_app.sh 即可。
exec "$LAUNCHER" "\$@" >>"\$HOME/.codex/model-switcher/launch.log" 2>&1
ENTRY
chmod +x "$APP_PATH/Contents/MacOS/launcher"

# 自签名，避免每次打开都被问一次
codesign --force --sign - "$APP_PATH" >/dev/null 2>&1 || true

echo "已生成：$APP_PATH"
echo "版本：v$TOOL_VERSION"
echo
echo "双击即可打开图形界面（后台运行，不弹终端窗口）。"
echo "首次打开如果被系统拦下，到「系统设置 → 隐私与安全性」里点「仍要打开」。"
echo
echo "想停掉后台服务：codex-switcher stop"
