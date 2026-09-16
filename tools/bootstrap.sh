#!/usr/bin/env bash
# 一条命令装好 codex（ChatGPT App）多平台模型切换。
#
#   curl -fsSL https://raw.githubusercontent.com/yankuanglin1-droid/codex-model-switcher/main/tools/bootstrap.sh | bash
#
# 装完直接可以：
#   codex-switcher add --preset deepseek --key-stdin
#   codex-switcher use deepseek
#
# 可调的环境变量（都不填也能跑）：
#   CODEX_SWITCHER_DIR      源码放哪（默认 ~/.local/share/codex-switcher-src）
#   CODEX_SWITCHER_WITH_APP macOS 上顺手装图形界面 App（默认 1，设 0 跳过）
#
# 为什么要有这个脚本：install.sh 需要仓库已经在本机（它要从仓库里复制代码），
# 所以「一条命令」必须先解决「把仓库弄下来」这一步。
set -euo pipefail

REPO_URL="${CODEX_SWITCHER_REPO:-https://github.com/yankuanglin1-droid/codex-model-switcher.git}"
SRC_DIR="${CODEX_SWITCHER_DIR:-$HOME/.local/share/codex-switcher-src}"
WITH_APP="${CODEX_SWITCHER_WITH_APP:-1}"

say() { printf '%s\n' "$*"; }
die() { printf '出错：%s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *)      OS="other" ;;
esac
say "系统：$OS"

# ---------------------------------------------------------------- 拿代码
if [ -f "$SRC_DIR/codex_switcher/__init__.py" ]; then
  say "已经有一份源码：$SRC_DIR"
  if [ -d "$SRC_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    say "尝试更新到最新…"
    git -C "$SRC_DIR" pull --ff-only --quiet 2>/dev/null || say "（更新失败，用现有的继续）"
  fi
else
  mkdir -p "$(dirname -- "$SRC_DIR")"
  if command -v git >/dev/null 2>&1; then
    say "下载源码到 $SRC_DIR …"
    git clone --depth 1 --quiet "$REPO_URL" "$SRC_DIR" || die "克隆失败，检查网络后重试"
  else
    # 没有 git 就下 tarball。注意这里用 main，不是某个版本号。
    tarball=$(mktemp -t codex-switcher).tar.gz
    say "本机没有 git，改用压缩包…"
    url="https://github.com/yankuanglin1-droid/codex-model-switcher/archive/refs/heads/main.tar.gz"
    curl -fsSL --retry 3 --retry-all-errors -o "$tarball" "$url" || die "下载失败，检查网络后重试"
    staging=$(mktemp -d)
    tar -xzf "$tarball" -C "$staging" || die "解压失败"
    rm -f "$tarball"
    mv "$staging"/codex-model-switcher-* "$SRC_DIR" 2>/dev/null || {
      first=$(find "$staging" -maxdepth 1 -mindepth 1 | head -1)
      mv "$first" "$SRC_DIR"
    }
    rm -rf "$staging"
  fi
fi

[ -f "$SRC_DIR/install.sh" ] || die "源码看起来不完整（找不到 install.sh）"

# ---------------------------------------------------------------- 装运行时
cd "$SRC_DIR"
say ""
say "安装运行时…"
if [ "$OS" = "macos" ] || [ "$OS" = "linux" ]; then
  bash ./install.sh
else
  die "这个引导脚本只处理 macOS / Linux；Windows 请用 PowerShell 版：
  git clone $REPO_URL
  cd codex-model-switcher
  powershell -ExecutionPolicy Bypass -File install.ps1"
fi

# ------------------------------------------------------------ 可选：图形界面
if [ "$OS" = "macos" ] && [ "$WITH_APP" != "0" ]; then
  say ""
  say "顺便装一个能双击打开的图形界面 App（自带 Python，约 1 分钟）…"
  if bash packaging/macos/build_app.sh --with-python >/dev/null 2>&1; then
    say "已装到：~/Applications/codex（ChatGPT App）多平台模型切换.app"
  else
    say "图形界面没装上（不影响命令行使用）。
想补装：cd \"$SRC_DIR\" && bash packaging/macos/build_app.sh --with-python"
  fi
fi

# ---------------------------------------------------------------- 下一步
cat <<'NEXT'

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
装好了。接下来只需要三步：

1) 接入你想要的那个平台（以 DeepSeek 为例）
     codex-switcher add --preset deepseek --key-stdin
   回车后粘贴 API Key，再按一次回车。密钥只进系统钥匙串。
   想先看有哪些内置平台：codex-switcher presets

2) 切过去
     codex-switcher use deepseek

3) 完全退出并重新打开 ChatGPT App / Codex（⌘Q / 完全关闭），
   然后新建一个任务，模型列表里就能选到它了。

切回官方 OpenAI：codex-switcher restore
自检环境：      codex-switcher doctor
图形界面：      codex-switcher app
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEXT
