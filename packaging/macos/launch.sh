#!/usr/bin/env bash
# macOS 应用入口：拉起协议桥与图形界面，然后打开浏览器。
# 由 .app 调用，也可以直接双击运行（需要先 chmod +x）。
set -uo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
STATE_DIR="${CODEX_HOME:-$HOME/.codex}/model-switcher"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/launch.log"

fail() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG"
  osascript -e "display alert \"Codex 多模型切换器\" message \"$1\" as critical" >/dev/null 2>&1
  exit 1
}

find_python() {
  # 1) 安装时记下的解释器
  if [ -f "$STATE_DIR/python-path" ]; then
    saved=$(cat "$STATE_DIR/python-path")
    if [ -x "$saved" ] && "$saved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s\n' "$saved"; return 0
    fi
  fi
  # 2) 显式指定
  if [ -n "${CODEX_SWITCHER_PYTHON:-}" ] && [ -x "${CODEX_SWITCHER_PYTHON}" ]; then
    printf '%s\n' "$CODEX_SWITCHER_PYTHON"; return 0
  fi
  # 3) 绝对路径（从访达启动时 PATH 很窄，command -v 找不到 Homebrew）
  for candidate in \
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.14 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 /usr/local/bin/python3; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  # 4) PATH 里找
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        command -v "$candidate"; return 0
      fi
    fi
  done
  return 1
}

PYTHON=$(find_python) || fail "找不到 Python 3.11 或更高版本。请先执行 brew install python@3.12，然后重新打开本应用。"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
printf '%s 使用解释器 %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PYTHON" >>"$LOG"

# 1) 协议桥：没在跑就后台拉起来（只影响需要翻译的平台）
if ! "$PYTHON" -c 'from codex_switcher import bridge; raise SystemExit(0 if bridge.is_running() else 1)' >/dev/null 2>&1; then
  nohup "$PYTHON" -m codex_switcher.bridge >>"$STATE_DIR/bridge.log" 2>&1 &
  sleep 0.3
fi

# 2) 图形界面已经在跑：app 命令会自己打开浏览器，然后退出
if "$PYTHON" -c 'from codex_switcher.webui import server; raise SystemExit(0 if server.existing_url() else 1)' >/dev/null 2>&1; then
  "$PYTHON" -m codex_switcher app >>"$STATE_DIR/gui.log" 2>&1
  exit 0
fi

# 3) 否则后台起一个新的。
#    必须后台运行：.app 里的 `do shell script` 会等前台命令结束，
#    如果这里前台跑服务，双击图标会一直卡住。
nohup "$PYTHON" -m codex_switcher app >>"$STATE_DIR/gui.log" 2>&1 &

# 等它把端口写出来（最多 6 秒），好让启动失败能被看见
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  [ -f "$STATE_DIR/gui.json" ] && break
  sleep 0.5
done

if [ -f "$STATE_DIR/gui.json" ]; then
  printf '%s 图形界面已启动\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG"
  exit 0
fi
fail "图形界面没有启动成功，详情见 $STATE_DIR/gui.log"
