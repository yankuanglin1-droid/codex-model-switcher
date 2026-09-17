#!/usr/bin/env bash
# 把图形界面打包成 macOS 应用。
#
#   bash packaging/macos/build_app.sh                         装到 ~/Applications
#   bash packaging/macos/build_app.sh /Applications           装到指定目录
#   bash packaging/macos/build_app.sh --with-python           打「完整版」：App 自带 Python
#   bash packaging/macos/build_app.sh --with-python /Applications
#
# 优先编译成**原生窗口应用**（Swift + WKWebView）：独立窗口、独立 Dock 图标，
# 不进浏览器。没有 swiftc 时退回到“壳 + 浏览器”方案。
#
# 标准版（默认）只带运行时代码，靠机器上已有的 Python 3.9+；
# 完整版（--with-python）额外带一份独立 Python，目标电脑上什么都不用装，双击就能用。
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
APP_NAME="${CODEX_SWITCHER_APP_NAME:-codex（ChatGPT App）多平台模型切换}"

# 解析参数。这里刻意不用数组 —— macOS 自带的是 bash 3.2，数组 + set -u 会报错。
BUNDLE_PYTHON="${CODEX_SWITCHER_BUNDLE_PYTHON:-0}"
OUT_DIR=""
for arg in "$@"; do
  case "$arg" in
    --with-python) BUNDLE_PYTHON=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    -*) echo "未知参数：$arg" >&2; exit 2 ;;
    *) OUT_DIR="$arg" ;;
  esac
done
OUT_DIR="${OUT_DIR:-$HOME/Applications}"
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
  echo "正在编译原生窗口应用（Swift + WKWebView，macOS 11+ 通用二进制）…"
  BUILD_DIR=$(mktemp -d)
  # 分别编 arm64 与 x86_64，再合成通用二进制 —— 这样一台 .app 通吃
  # Apple Silicon 和 Intel 的 Mac。交叉编译失败就退回本机架构。
  for arch in arm64 x86_64; do
    if MACOSX_DEPLOYMENT_TARGET=11.0 xcrun swiftc -O -target "${arch}-apple-macos11.0" \
        -framework Cocoa -framework WebKit \
        -o "$BUILD_DIR/$arch" "$SWIFT_SOURCE" 2>"$BUILD_DIR/$arch.log"; then
      :
    else
      echo "  $arch 切片编译失败（换架构时可能缺 SDK），稍后退回本机架构"
      rm -f "$BUILD_DIR/$arch"
    fi
  done
  if [ -f "$BUILD_DIR/arm64" ] && [ -f "$BUILD_DIR/x86_64" ]; then
    lipo -create -output "$APP_PATH/Contents/MacOS/CodexSwitcherApp" \
      "$BUILD_DIR/arm64" "$BUILD_DIR/x86_64" && NATIVE=1
    echo "  已合成通用二进制：$(lipo -archs "$APP_PATH/Contents/MacOS/CodexSwitcherApp" 2>/dev/null)"
  else
    ARCH=$(uname -m)
    if MACOSX_DEPLOYMENT_TARGET=11.0 xcrun swiftc -O -target "${ARCH}-apple-macos11.0" \
        -framework Cocoa -framework WebKit \
        -o "$APP_PATH/Contents/MacOS/CodexSwitcherApp" "$SWIFT_SOURCE" 2>"$BUILD_DIR/fallback.log"; then
      NATIVE=1
      echo "  仅本机架构：$ARCH"
    else
      echo "编译失败，退回浏览器方案。日志："
      tail -8 "$BUILD_DIR"/fallback.log 2>/dev/null || true
    fi
  fi
  rm -rf "$BUILD_DIR"
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
	<string>11.0</string>
	<key>LSMultipleInstancesProhibited</key>
	<true/>
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

# 完整版：把独立 Python 塞进包里，目标电脑什么都不用装
BUNDLED_ARCHS=""
if [ "$BUNDLE_PYTHON" = "1" ]; then
  PYRT_SRC="${CODEX_SWITCHER_PYTHON_RUNTIME:-$REPO_ROOT/packaging/macos/python-runtime}"
  if [ ! -x "$PYRT_SRC/python-arm64/bin/python3" ] && [ ! -x "$PYRT_SRC/python-x86_64/bin/python3" ]; then
    echo "正在准备内置 Python（只下一次，之后复用）…"
    bash "$REPO_ROOT/packaging/macos/fetch_python_runtime.sh" "$PYRT_SRC" || true
  fi
  for arch in arm64 x86_64; do
    if [ -x "$PYRT_SRC/python-$arch/bin/python3" ]; then
      rm -rf "$RUNTIME_DIR/python-$arch"
      # ditto 会保留符号链接与可执行权限，cp -R 在个别文件上会走样
      ditto "$PYRT_SRC/python-$arch" "$RUNTIME_DIR/python-$arch"
      BUNDLED_ARCHS="${BUNDLED_ARCHS:+$BUNDLED_ARCHS + }$arch"
    else
      echo "  缺少 $arch 的内置 Python，该架构会回退到系统 Python"
    fi
  done
  if [ -n "$BUNDLED_ARCHS" ]; then
    # 先把字节码缓存清干净，再签名 —— 顺序不能反。
    # 内置 Python 的标准库里带 __pycache__；如果在签完名之后才删，包体内容变了，
    # 外层签名立刻失效（下载回来会被 macOS 判成「已损坏」）。
    # package_release.sh 也会清一次缓存，这里清掉之后它就没东西可删，签名保持有效。
    find "$RUNTIME_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$RUNTIME_DIR" -name "*.pyc" -delete 2>/dev/null || true
    # 内嵌的可执行文件必须是合法签名才能在 Apple 芯片上运行；
    # 随包复制后签名会失效，所以要重新签一遍。
    while IFS= read -r bin; do
      [ -n "$bin" ] || continue
      codesign --force --sign - "$bin" >/dev/null 2>&1 || true
    done <<EOF
$(find "$RUNTIME_DIR" -path "*/python-*/*" -type f -exec sh -c 'file -b "$1" | grep -q Mach-O && printf "%s\n" "$1"' _ {} \; 2>/dev/null)
EOF
  fi
fi

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
if [ -n "$BUNDLED_ARCHS" ]; then
  echo "自带运行时：是（不需要另外 clone 仓库）"
  echo "自带 Python：$BUNDLED_ARCHS —— 目标电脑不用装 Python"
else
  echo "自带运行时：是（不需要另外 clone 仓库）"
  echo "Python：用目标电脑上的 Python 3.9+（要开箱即用就打完整版，见 --help）"
fi
echo
echo "双击即可打开图形界面。"
echo "首次打开如果被系统拦下，到「系统设置 → 隐私与安全性」里点「仍要打开」。"
echo
echo "想停掉后台服务：codex-switcher stop"
