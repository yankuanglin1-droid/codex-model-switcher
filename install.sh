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
  # 先看绝对路径：终端 PATH 被改窄时（比如在某些 IDE 里跑），command -v 找不到
  # Homebrew 或 python.org 装的解释器，但它们其实在。
  for candidate in \
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.9/bin/python3 \
    /usr/bin/python3; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
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
    say "两条免费做法：一、运行 xcode-select --install；二、到 python.org 下载安装包。"
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

  # 装完就验一遍链路，别等用户跑起来才发现"加载不出来 / 一直重连"。
  #
  # 这一环以前是缺的：安装脚本只负责把东西放好，至于「放好之后到底能不能
  # 用」，要等用户配上平台、切过去、发第一条消息才知道 —— 而那时报出来的
  # 错（反复重连、订阅无法使用第三方模型）跟安装阶段的问题对不上号，
  # 排查起来全靠猜。现在装完当场验，有问题立刻看得见。
  #
  # 这里刻意不因检查失败而中断安装：没有 Codex 配置文件、还没添加平台
  # 都属正常，不该让安装"失败"。
  say ""
  if "$TARGET_DIR/codex-switcher" check; then :; else
    say ""
    say "（上面这几条不是安装失败，是提醒：现在还没法正常请求。）"
    say "配好平台之后再跑一次 codex-switcher check 复查。"
  fi

  say ""
  say "完成。接下来："
  say "  1) codex-switcher add --preset deepseek --key-stdin   # 添加平台"
  say "  2) codex-switcher use deepseek                        # 切换"
  say "  3) codex-switcher check                               # 验一遍链路"
  say "  4) 完全退出并重新打开 Codex"
  say "也可以直接运行 codex-switcher app 打开图形界面。"
fi
