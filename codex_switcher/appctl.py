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

import os
import shutil
import signal
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
QUIT_TIMEOUT_SECONDS = 25.0
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


def _pids() -> List[int]:
    """当前桌面端进程 PID 集合（Codex / ChatGPT 双名合并）。"""
    if shutil.which("pgrep") is None:
        return []
    pids: List[int] = []
    for name in APP_CANDIDATE_NAMES:
        try:
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                pids.extend(int(x) for x in result.stdout.split())
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
    return pids


def _terminate_gracefully() -> bool:
    """SIGTERM 软杀：等价于系统层 ⌘Q，给进程存档机会。

    AppleScript 遇到两类墙时用：本机辅助访问权限被关（-1719/-1728）、
    App 自己弹窗挡住 quit 事件（用户已取消 -128）。SIGTERM 不受这两者影响。
    """
    pids = _pids()
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + QUIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not _pids():
            return True
        time.sleep(POLL_INTERVAL)
    return not _pids()


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
        # 退出三级降级，全都等价于 ⌘Q 的「给 Codex 存状态的机会」：
        #   1) AppleScript（bundle id）—— 最正规，走 App 自己的退出流程；
        #   2) AppleScript（app 名）—— 老版本兜底；
        #   3) SIGTERM 软杀 —— 上两级被辅助访问权限/App 弹窗挡住时用。
        # 三级全失败才让用户手动 ⌘Q，绝不 silent 跳过退出直接 open。
        quit_ok = _osascript(
            'tell application id "%s" to quit' % APP_BUNDLE_ID)
        if not quit_ok:
            quit_ok = _osascript('tell application "%s" to quit' % APP_NAME)
        if quit_ok:
            result["method"] = "quit-and-reopen"
        else:
            if _terminate_gracefully():
                result["method"] = "term-and-reopen"
            else:
                result["detail"] = (
                    "无法自动退出 Codex（AppleScript 与软杀都失败，"
                    "可能被 App 内弹窗挡住），请手动 ⌘Q 后重开。")
                return result
        deadline = time.time() + QUIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not is_running():
                break
            time.sleep(POLL_INTERVAL)
        else:
            result["detail"] = "Codex 在 %g 秒内没有退出，请手动 ⌘Q 后重开。" % QUIT_TIMEOUT_SECONDS
            return result
    else:
        result["method"] = "open"

    if _open_app(app):
        result["restarted"] = True
        result["detail"] = "Codex 已重启，新模型即刻生效。"
    else:
        result["detail"] = "无法自动拉起 Codex（open 失败），请手动打开。"
    return result
