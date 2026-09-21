"""Codex 桌面应用的定位与优雅重启。

为什么需要它：切换模型后，Codex 桌面版要把新配置「吃进去」必须完全退出
再重开 —— 配置和任务绑定都缓存在进程里，这是 Codex 自己的设计，工具侧
绕不开。绕得开的是**手动 ⌘Q + 找图标再点开**这两步：由工具优雅退出、
等进程退干净、再按 bundle id 拉起来。

按 bundle id（com.openai.codex）定位而不是显示名：实测这台机器上它是
/Applications/ChatGPT.app（OpenAI 把桌面版显示名改成了 ChatGPT，
bundle id 没变），显示名以后可能再改，bundle id 是稳定的。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

BUNDLE_ID = "com.openai.codex"

# 常见安装位置。显示名可能变（ChatGPT/Codex），用 Info.plist 里的
# bundle id 做最终确认。
CANDIDATES = [
    "/Applications/ChatGPT.app",
    "/Applications/Codex.app",
    "~/Applications/ChatGPT.app",
    "~/Applications/Codex.app",
]

QUIT_TIMEOUT_SECONDS = 12


def find_app() -> Optional[str]:
    """找到 Codex 桌面 App 的路径；找不到返回 None。"""
    if sys.platform != "darwin":
        return None
    for candidate in CANDIDATES:
        path = Path(candidate).expanduser()
        plist = path / "Contents" / "Info.plist"
        try:
            if plist.exists() and BUNDLE_ID in plist.read_text(
                    encoding="utf-8", errors="replace"):
                return str(path)
        except OSError:
            continue
    return None


def _osascript(script: str) -> bool:
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script],
                                capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def is_running() -> bool:
    return _osascript('application id "%s" is running' % BUNDLE_ID)


def _quit(timeout: int = QUIT_TIMEOUT_SECONDS) -> bool:
    """优雅退出。用户在 Codex 里点了不允许退出的弹窗时，等超时放弃。"""
    if not _osascript('tell application id "%s" to quit' % BUNDLE_ID):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.5)
    return not is_running()


def _reopen() -> bool:
    try:
        result = subprocess.run(["/usr/bin/open", "-b", BUNDLE_ID],
                                capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def restart(quit_timeout: int = QUIT_TIMEOUT_SECONDS) -> Dict:
    """优雅重启 Codex 桌面版，让它重新读配置。"""
    if sys.platform != "darwin":
        return {"ok": False, "reason": "unsupported",
                "detail": "目前只有 macOS 有桌面版可重启"}
    app = find_app()
    if not app:
        return {"ok": False, "reason": "not-found",
                "detail": "没找到 Codex 桌面应用（ChatGPT.app / Codex.app）"}
    was_running = is_running()
    if was_running and not _quit(quit_timeout):
        return {"ok": False, "reason": "quit-timeout",
                "detail": "Codex 没有在 %d 秒内退出（可能有未保存的弹窗），请手动 ⌘Q" % quit_timeout}
    if not _reopen():
        return {"ok": False, "reason": "reopen-failed",
                "detail": "没能重新拉起 Codex，请手动打开"}
    return {"ok": True, "app": app, "was_running": was_running}
