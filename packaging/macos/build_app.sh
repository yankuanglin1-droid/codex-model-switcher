#!/usr/bin/env bash
# 把图形界面打包成 macOS 应用。
#
#   bash packaging/macos/build_app.sh                    装到 ~/Applications
#   bash packaging/macos/build_app.sh /Applications      装到指定目录
#
# 优先编译成**原生窗口应用**（Swift + WKWebView）：独立窗口、独立 Dock 图标，
# 不进浏览器。没有 swiftc 时退回到“壳 + 浏览器”方案。
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
APP_NAME="${CODEX_SWITCHER_APP_NAME:-codex（ChatGPT App）多平台模型切换}"
OUT_DIR="${1:-$HOME/Applications}"
APP_PATH="$OUT_DIR/$APP_NAME.app"
APP_HOME="${CODEX_SWITCHER_HOME:-$HOME/.local/share/codex-switcher}"
LAUNCHER="$APP_HOME/packaging/macos/launch.sh"
SWIFT_SOURCE="$REPO_ROOT/packaging/macos/CodexSwitcherApp.swift"
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

NATIVE=0
if command -v xcrun >/dev/null 2>&1 && xcrun --find swiftc >/dev/null 2>&1 && [ -f "$SWIFT_SOURCE" ]; then
  ARCH=$(uname -m)
  echo "正在编译原生窗口应用（Swift + WKWebView）…"
  if MACOSX_DEPLOYMENT_TARGET=12.0 xcrun swiftc -O \
      -target "${ARCH}-apple-macos12.0" \
      -framework Cocoa -framework WebKit \
      -o "$APP_PATH/Contents/MacOS/CodexSwitcherApp" \
      "$SWIFT_SOURCE" 2>/tmp/codex-switcher-swift-build.log; then
    NATIVE=1
  else
    echo "编译失败，退回浏览器方案。日志："
    tail -8 /tmp/codex-switcher-swift-build.log || true
  fi
fi

if [ "$NATIVE" -eq 1 ]; then
  EXECUTABLE="CodexSwitcherApp"
  TRANSPORT_NOTE="原生窗口"
else
  EXECUTABLE="launcher"
  TRANSPORT_NOTE="浏览器窗口"
  cat > "$APP_PATH/Contents/MacOS/launcher" <<ENTRY
#!/bin/sh
# swiftc 不可用时的后备方案：调用启动脚本，界面开在浏览器里。
exec "$LAUNCHER" "\$@" >>"\$HOME/.codex/model-switcher/launch.log" 2>&1
ENTRY
  chmod +x "$APP_PATH/Contents/MacOS/launcher"
fi

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
	<string>$EXECUTABLE</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleShortVersionString</key>
	<string>$TOOL_VERSION</string>
	<key>CFBundleVersion</key>
	<string>$TOOL_VERSION</string>
	<key>LSMinimumSystemVersion</key>
	<string>12.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSAppTransportSecurity</key>
	<dict>
		<key>NSAllowsLocalNetworking</key>
		<true/>
	</dict>
</dict>
</plist>
PLIST

# 自签名，避免每次打开都被问一次
RUNTIME_DIR="$APP_PATH/Contents/Resources/runtime"
mkdir -p "$RUNTIME_DIR/packaging/macos"
cp -R "$REPO_ROOT/codex_switcher" "$RUNTIME_DIR/codex_switcher"
find "$RUNTIME_DIR/codex_switcher" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
cp "$REPO_ROOT/packaging/macos/launch.sh" "$RUNTIME_DIR/packaging/macos/launch.sh"
chmod +x "$RUNTIME_DIR/packaging/macos/launch.sh"

# 应用图标：黑白液态玻璃的 Codex 标
ICON_SRC="$REPO_ROOT/packaging/macos/icon/AppIcon.icns"
if [ ! -f "$ICON_SRC" ]; then
  echo "图标还没生成，正在补生成…"
  python3 "$REPO_ROOT/packaging/macos/icon/make_icon.py" >/dev/null 2>&1 || true
fi
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APP_PATH/Contents/Resources/AppIcon.icns"
fi

codesign --force --sign - "$APP_PATH" >/dev/null 2>&1 || true

echo "已生成：$APP_PATH"
echo "版本：v$TOOL_VERSION"
echo "形式：$TRANSPORT_NOTE"
echo "自带运行时：是（不需要另外 clone 仓库）"
echo
echo "双击即可打开图形界面。"
echo "首次打开如果被系统拦下，到「系统设置 → 隐私与安全性」里点「仍要打开」。"
echo
echo "想停掉后台服务：codex-switcher stop"
