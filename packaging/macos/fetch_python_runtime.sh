#!/usr/bin/env bash
# 给 macOS .app 内置一份独立 Python —— 有了它，目标电脑上完全不用装 Python，
# 也不用装 Homebrew / Xcode 命令行工具，双击即可用。
#
#   bash packaging/macos/fetch_python_runtime.sh <目标目录>
#
# 目标目录一般传 .app 里的运行时目录，例如：
#   .../ChatGPT Model Switcher.app/Contents/Resources/runtime
#
# 结果：<目标目录>/python-arm64/bin/python3 和 <目标目录>/python-x86_64/bin/python3
# launch.sh 会在启动时按 uname -m 自动挑对应的那一份。
#
# 依赖：只需要网络。走 api.github.com 直连（GitHub 网页端在国内经常断，
# 但 API 与对象存储通常可达）；脚本自带重试，中断后重跑会继续。
set -uo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "用法：bash packaging/macos/fetch_python_runtime.sh <目标目录>" >&2
  exit 2
fi

# 固定版本，保证每次打出来的 App 一致；想换版本用 PYBS_TAG 覆盖。
PYBS_REPO="${PYBS_REPO:-astral-sh/python-build-standalone}"
PYBS_TAG="${PYBS_TAG:-20260901}"
PYTHON_SERIES="${PYTHON_SERIES:-3.12}"

mkdir -p "$TARGET"

api() { curl -sS -m 60 -H "Accept: application/vnd.github+json" "$@"; }

echo "查询 $PYBS_REPO 的 $PYBS_TAG 发布信息…"
RELEASE_JSON=""
for attempt in 1 2 3 4 5; do
  RELEASE_JSON=$(api "https://api.github.com/repos/$PYBS_REPO/releases/tags/$PYBS_TAG" 2>/dev/null)
  # 网络抖动时 curl 可能返回 0 但内容为空，所以这里按内容判断，不只看退出码。
  # 注意：这里不能用 `printf ... | grep -q` —— 开了 pipefail 之后 grep 一命中就退出，
  # printf 收到 SIGPIPE 会让整条管道返回失败，把好数据误判成空。
  case "$RELEASE_JSON" in
    *'"assets"'*) break ;;
  esac
  echo "  第 $attempt 次没拿到发布信息，等 3 秒重试…" >&2
  sleep 3
  RELEASE_JSON=""
done
if [ -z "$RELEASE_JSON" ]; then
  echo "连不上 api.github.com 或返回为空，请检查网络后重试。" >&2
  exit 1
fi

# 找出两个架构各自的 asset id：只要 install_only_stripped（体积小一半左右）。
# 注意：这里用 -c 而不是 heredoc —— 管道和 heredoc 会抢同一个 stdin，
# 那样 python 读到的是空内容。JSON 从标准输入进，脚本从 -c 进，互不干扰。
ASSETS=$(printf '%s' "$RELEASE_JSON" | python3 -c '
import json, sys
series = sys.argv[1]
data = json.load(sys.stdin)
want = {}
for a in data.get("assets", []):
    name = a["name"]
    if "install_only_stripped.tar.gz" not in name or "apple-darwin" not in name:
        continue
    if "universal2" in name or not name.startswith("cpython-%s." % series):
        continue
    if "aarch64" in name:
        want["arm64"] = (a["id"], name)
    elif "x86_64" in name:
        want["x86_64"] = (a["id"], name)
for arch in ("arm64", "x86_64"):
    if arch in want:
        print(arch, want[arch][0], want[arch][1])
' "$PYTHON_SERIES")

if [ -z "$ASSETS" ]; then
  echo "在 $PYBS_TAG 里没找到 $PYTHON_SERIES 的 macOS 独立 Python 包。" >&2
  exit 1
fi

ok=0
while read -r arch aid name; do
  [ -n "${arch:-}" ] || continue
  dest="$TARGET/python-$arch"
  marker="$dest/bin/python3"
  if [ -x "$marker" ] && "$marker" -c 'import sys' >/dev/null 2>&1; then
    echo "  ${arch} 已经有可用的解释器，跳过"
    ok=$((ok + 1))
    continue
  fi
  tarball=$(mktemp -t pybs).tar.gz
  echo "  下载 ${arch}：${name}"
  if ! curl -sSL --retry 5 --retry-delay 3 --retry-all-errors -C - -m 1800 \
      -H "Accept: application/octet-stream" \
      -o "$tarball" \
      "https://api.github.com/repos/$PYBS_REPO/releases/assets/$aid"; then
    echo "  ${arch} 下载失败，稍后重跑本脚本会继续。" >&2
    continue
  fi
  echo "  解包 ${arch}…"
  staging=$(mktemp -d)
  if ! tar -xzf "$tarball" -C "$staging"; then
    echo "  ${arch} 解包失败（文件可能没下全），已跳过。" >&2
    rm -rf "$staging" "$tarball"
    continue
  fi
  # 包内结构固定是 python/bin/python3
  rm -rf "$dest"
  mv "$staging/python" "$dest"
  rm -rf "$staging" "$tarball"
  if [ -x "$marker" ] && "$marker" -c 'import sys' >/dev/null 2>&1; then
    echo "  ✅ ${arch} 就绪：$("$marker" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
    ok=$((ok + 1))
  else
    echo "  ❌ $arch 装好了但跑不起来，已删除" >&2
    rm -rf "$dest"
  fi
done <<<"$ASSETS"

if [ "$ok" -eq 2 ]; then
  echo "内置 Python 完成：arm64 + x86_64 都可用。这个 App 不再依赖用户机器上的 Python。"
else
  echo "只成功 $ok/2 个架构。App 仍可用，但缺少的架构会回退到系统 Python。" >&2
  exit 1
fi
