#!/usr/bin/env bash
# 把编译好的 .app 打成发布用的 zip。
#
#   bash packaging/macos/package_release.sh                 # 标准版（~1 MB）
#   bash packaging/macos/package_release.sh --full          # 完整版（自带 Python）
#   bash packaging/macos/package_release.sh --full --app /path/to/x.app
#
# 文件名只在这里定义一次。之前是发布时手打的，结果文档里写的名字和实际
# 上传的名字对不上，用户照着文档根本找不到文件。现在统一从这里出。
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
APP_NAME="${CODEX_SWITCHER_APP_NAME:-codex（ChatGPT App）多平台模型切换}"
OUT_DIR="$REPO_ROOT/dist"

MODE="standard"
APP_PATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --app) APP_PATH="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ -z "$APP_PATH" ]; then
  APP_PATH="$HOME/Applications/$APP_NAME.app"
fi

if [ ! -d "$APP_PATH" ]; then
  echo "找不到 App：$APP_PATH" >&2
  echo "先跑：bash packaging/macos/build_app.sh${MODE:+ }$([ "$MODE" = full ] && echo --with-python)" >&2
  exit 1
fi

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_ROOT/codex_switcher/__init__.py" | head -1)
[ -n "$VERSION" ] || { echo "读不到版本号" >&2; exit 1; }

SUFFIX=""
[ "$MODE" = "full" ] && SUFFIX="-full"
ZIP="$OUT_DIR/Codex-Model-Switcher-macOS-v$VERSION$SUFFIX.zip"

mkdir -p "$OUT_DIR"
[ -e "$ZIP" ] && rm -f "$ZIP"

ditto -c -k --keepParent "$APP_PATH" "$ZIP"

# 顺手核对一下：完整版必须真的带 Python，别打出个假的「完整版」
if [ "$MODE" = "full" ]; then
  # 先把清单落到变量里再判断。不能写成 `unzip -l … | grep -q …`：开了 pipefail 之后
  # grep 一命中就退出，unzip 收到 SIGPIPE 会让整条管道失败，把好包误判成坏包。
  LISTING=$(unzip -l "$ZIP")
  case "$LISTING" in
    *"runtime/python-arm64/bin/python3"*) : ;;
    *)
    echo "⚠ 这个包里没有内置 Python，不像完整版，请检查构建步骤。" >&2
    exit 1
      ;;
  esac
fi

SIZE=$(du -h "$ZIP" | cut -f1)
SHA=$(shasum -a 256 "$ZIP" | cut -d' ' -f1)
echo "已生成：$ZIP"
echo "  体积：$SIZE"
echo "  sha256：$SHA"
