#!/usr/bin/env bash
# Codex Model Switcher 一键安装（macOS / Linux）
#
#   bash install.sh             安装到 ~/.local/bin 并初始化
#   bash install.sh --no-init   只装命令，不做初始化
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR="${CODEX_SWITCHER_BIN_DIR:-$HOME/.local/bin}"
# 运行时目录：故意放在 ~/.local/share 而不是仓库所在的 ~/Documents。
# macOS 会保护 文稿/桌面/下载 这几个目录，从访达启动的 .app 无权执行里面的脚本。
APP_HOME="${CODEX_SWITCHER_HOME:-$HOME/.local/share/codex-switcher}"
INIT=1

for arg in "$@"; do
  case "$arg" in
    --no-init) INIT=0 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
  esac
done

say() { printf '%s\n' "$*"; }

find_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      # 3.9 也能跑：没有内置 tomllib 时用逐行校验兜底
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON=$(find_python || true)
if [ -z "$PYTHON" ]; then
  say "需要 Python 3.9 或更高版本（推荐 3.11+）。"
  if command -v brew >/dev/null 2>&1; then
    say "检测到 Homebrew，可以运行：brew install python@3.12"
  else
    say "请先安装 Python 3.9+ 后重新运行本脚本。"
  fi
  exit 1
fi
say "使用 Python：$PYTHON"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  say "提示：这个 Python 低于 3.11，配置文件校验会走降级路径；升级到 3.11+ 更稳。"
fi

mkdir -p "$TARGET_DIR"

# 把包复制到运行时目录（不复制仓库里的其它文件，避免把开发产物带过去）
rm -rf "$APP_HOME/codex_switcher"
mkdir -p "$APP_HOME/packaging/macos"
cp -R "$REPO_ROOT/codex_switcher" "$APP_HOME/codex_switcher"
cp "$REPO_ROOT/packaging/macos/launch.sh" "$APP_HOME/packaging/macos/launch.sh"
chmod +x "$APP_HOME/packaging/macos/launch.sh"
say "已安装运行时：$APP_HOME"

cat > "$TARGET_DIR/codex-switcher" <<EOF
#!/bin/sh
set -e
export CODEX_SWITCHER_PYTHON="$PYTHON"
export PYTHONPATH="$APP_HOME\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON" -m codex_switcher "\$@"
EOF
chmod +x "$TARGET_DIR/codex-switcher"
say "已安装命令：$TARGET_DIR/codex-switcher"

case ":$PATH:" in
  *":$TARGET_DIR:"*) ;;
  *)
    say ""
    say "提示：$TARGET_DIR 不在 PATH 里，请把下面一行加到 ~/.zshrc 或 ~/.bashrc："
    say "  export PATH=\"$TARGET_DIR:\$PATH\""
    ;;
esac

if [ "$INIT" -eq 1 ]; then
  say ""
  "$TARGET_DIR/codex-switcher" init
  mkdir -p "${CODEX_HOME:-$HOME/.codex}/model-switcher"
  printf '%s\n' "$PYTHON" > "${CODEX_HOME:-$HOME/.codex}/model-switcher/python-path"
  printf '%s\n' "$APP_HOME" > "${CODEX_HOME:-$HOME/.codex}/model-switcher/runtime-path"
  say ""
  say "完成。接下来："
  say "  1) codex-switcher add --preset deepseek --key-stdin   # 添加平台"
  say "  2) codex-switcher use deepseek                        # 切换"
  say "  3) 完全退出并重新打开 Codex"
  say "也可以直接运行 codex-switcher app 打开图形界面。"
fi
