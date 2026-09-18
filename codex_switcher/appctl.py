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
# 2026-09: Codex 桌面端并入 ChatGPT.app —— bundle id 仍是 com.openai.codex，
# 进程名和 .app 名都变成了 "ChatGPT"。只认 app 名会把新版识别成「CLI 形态」，
# 一键重启直接落空。识别一律走 bundle id，app 名只是启动时的参考。
APP_BUNDLE_ID = "com.openai.codex"
APP_CANDIDATE_NAMES = ("Codex", "ChatGPT")
QUIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL = 0.3


def _app_candidates() -> List[Path]:
    home = Path.home()
    names: List[Path] = []
    for name in APP_CANDIDATE_NAMES:
        names.append(Path("/Applications") / (name + ".app"))
        names.append(home / "Applications" / (name + ".app"))
    return names


def _bundle_id(app_path: Path) -> Optional[str]:
    """读 app 的 CFBundleIdentifier；读不到（没有 Info.plist）返回 None。"""
    plist = app_path / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        result = subprocess.run(
            ["defaults", "read", str(plist), "CFBundleIdentifier"],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def find_app() -> Optional[Path]:
    """按 bundle id 找 Codex 桌面端（无论它叫 Codex.app 还是 ChatGPT.app）。"""
    for candidate in _app_candidates():
        if candidate.is_dir() and _bundle_id(candidate) == APP_BUNDLE_ID:
            return candidate
    return None


def is_running() -> bool:
    if shutil.which("pgrep") is None:
        return False
    # 进程名可能是 Codex 或 ChatGPT（并入 ChatGPT.app 之后），逐个试
    for name in APP_CANDIDATE_NAMES:
        try:
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def _osascript(script: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _open_app(app_path: Optional[Path] = None) -> bool:
    try:
        command = ["open", "-a", APP_NAME]
        if app_path is not None:
            # 新版桌面端叫 ChatGPT.app：用完整路径拉起，别赌 app 名
            command = ["open", str(app_path)]
        result = subprocess.run(
            command,
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
        # 优雅退出，等价于 ⌘Q：给 Codex 存状态的机会，别硬杀。
        # bundle id 是唯一可靠锚点：app 叫 Codex 还是 ChatGPT 都能退。
        quit_ok = _osascript(
            'tell application id "%s" to quit' % APP_BUNDLE_ID)
        if not quit_ok:
            quit_ok = _osascript('tell application "%s" to quit' % APP_NAME)
        if not quit_ok:
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

    if _open_app(app):
        result["restarted"] = True
        result["detail"] = "Codex 已重启，新模型即刻生效。"
    else:
        result["detail"] = "无法自动拉起 Codex（open 失败），请手动打开。"
    return result
