"""检测并重启 Codex 桌面 App。

为什么需要它：Codex 只在启动时读一次 ``config.toml``。切换模型后不重启，
跑着的还是旧平台；以前靠用户记得 ⌘Q 重开，现在切换成功的弹窗里直接给
「重启 Codex」按钮，点一下由本模块代劳。

做法（macOS）：
  1. 找 Codex.app（/Applications 或 ~/Applications）；
  2. 没找到 → Codex 多半是 CLI 形态，新会话天然读新配置，无需重启，如实说明；
  3. 找到且在运行 → AppleScript 优雅退出（等价于 ⌘Q，给 Codex 存档机会），
     轮询等它退干净，再 ``open -a Codex`` 拉起来；
  4. 找到但没在运行 → 直接 open。

任何失败都只返回说明，不抛错 —— 重启失败不能把切换本身变成事故。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

APP_NAME = "Codex"
QUIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL = 0.3


def _app_candidates() -> List[Path]:
    home = Path.home()
    return [
        Path("/Applications") / (APP_NAME + ".app"),
        home / "Applications" / (APP_NAME + ".app"),
    ]


def find_app() -> Optional[Path]:
    for candidate in _app_candidates():
        if candidate.is_dir():
            return candidate
    return None


def is_running() -> bool:
    if shutil.which("pgrep") is None:
        return False
    try:
        result = subprocess.run(
            ["pgrep", "-x", APP_NAME],
            capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _osascript(script: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _open_app() -> bool:
    try:
        result = subprocess.run(
            ["open", "-a", APP_NAME],
            capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def status() -> Dict:
    app = find_app()
    return {"app": str(app) if app else None,
            "running": is_running() if app else False}


def restart_codex() -> Dict:
    """重启 Codex 桌面 App。返回人话说明，绝不抛错。"""
    result = {"restarted": False, "detail": "", "method": None}
    app = find_app()
    if app is None:
        result["detail"] = (
            "没有找到 %s.app：Codex 以 CLI 形态运行时，新会话自动读取新配置，"
            "无需重启。" % APP_NAME)
        return result

    if is_running():
        # 优雅退出，等价于 ⌘Q：给 Codex 存状态的机会，别硬杀
        if not _osascript('tell application "%s" to quit' % APP_NAME):
            result["detail"] = "无法向 Codex 发送退出指令（AppleScript 失败），请手动 ⌘Q 后重开。"
            return result
        deadline = time.time() + QUIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not is_running():
                break
            time.sleep(POLL_INTERVAL)
        else:
            result["detail"] = "Codex 在 %g 秒内没有退出，请手动 ⌘Q 后重开。" % QUIT_TIMEOUT_SECONDS
            return result
        result["method"] = "quit-and-reopen"
    else:
        result["method"] = "open"

    if _open_app():
        result["restarted"] = True
        result["detail"] = "Codex 已重启，新模型即刻生效。"
    else:
        result["detail"] = "无法自动拉起 Codex（open -a 失败），请手动打开。"
    return result
