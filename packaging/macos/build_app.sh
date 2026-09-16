#!/usr/bin/env bash
# 把命令行工具打包成 macOS 可双击的 .app（内部调用 codex-switcher app）
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
APP_NAME="${CODEX_SWITCHER_APP_NAME:-Codex 多模型切换器}"
OUT_DIR="${1:-$REPO_ROOT/dist}"
APP_PATH="$OUT_DIR/$APP_NAME.app"

mkdir -p "$OUT_DIR"
rm -rf "$APP_PATH"

osacompile -o "$APP_PATH" - <<APPLESCRIPT
on run
	set repoRoot to "$REPO_ROOT"
	set launcher to repoRoot & "/bin/codex-switcher"
	tell application "Terminal"
		activate
		do script quoted form of launcher & " app"
	end tell
end run
APPLESCRIPT

echo "已生成：$APP_PATH"
echo "双击即可打开图形界面（会在终端里运行一个仅本机可访问的服务）。"
