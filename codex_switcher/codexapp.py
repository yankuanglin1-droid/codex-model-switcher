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

import plistlib
import os
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
            if plist.exists() and plistlib.loads(plist.read_bytes()).get("CFBundleIdentifier") == BUNDLE_ID:
                return str(path)
        except (OSError, ValueError, plistlib.InvalidFileException):
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
    result = subprocess.run(["/usr/bin/osascript", "-e",
                             'application id "%s" is running' % BUNDLE_ID],
                            capture_output=True, text=True, timeout=20)
    if result.returncode or result.stdout.strip().lower() not in ("true", "false"):
        raise RuntimeError("Cannot verify whether ChatGPT/Codex has exited")
    return result.stdout.strip().lower() == "true"


def _storage_files(home: Path):
    databases = sorted(set(Path(home).glob('state_*.sqlite')) |
                       set(Path(home).glob('thread_history_*.sqlite')))
    files = {}
    for database in databases:
        for path in (database, Path(str(database) + '-wal'), Path(str(database) + '-shm')):
            try:
                info = path.stat()
            except FileNotFoundError:
                if path == database:
                    raise OSError('History database changed during shutdown verification') from None
                continue
            files[str(path)] = (info.st_dev, info.st_ino)
    return tuple(str(path) for path in databases), files


def assert_history_idle(home: Path) -> None:
    """Refuse old host workers or external database handles after the UI exits.

    Only this recovery process and its children (private read-only replay
    clients) are allowed. A failed process/handle probe never means idle.
    """
    if sys.platform != 'darwin':
        raise OSError('Automatic storage shutdown verification requires macOS')
    expected_databases, _ = _storage_files(home)
    for _ in range(3):
        if _history_idle_probe(home, expected_databases):
            return
    raise OSError('History storage changed during shutdown verification')


def _history_idle_probe(home: Path, expected_databases) -> bool:
    """Require a clean probe of a stable file set, including after sidecar churn.

    SQLite may remove WAL/SHM between enumeration and lsof. Retry that race from
    a fresh process inventory; never ignore stderr or accept the failed probe.
    The main database set is not allowed to disappear or change.
    """
    process_list = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,command='],
                                  capture_output=True, text=True, timeout=20)
    if process_list.returncode:
        raise OSError('Cannot verify remaining host processes')
    processes = {}
    for line in process_list.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise OSError('Unexpected process inventory')
        processes[int(parts[0])] = (int(parts[1]), parts[2])
    allowed = {os.getpid()}
    while True:
        expanded = allowed | {pid for pid, (parent, _) in processes.items() if parent in allowed}
        if expanded == allowed:
            break
        allowed = expanded
    application = find_app()
    if not application:
        raise OSError('Cannot identify desktop host installation')
    for pid, (_, command) in processes.items():
        if pid not in allowed and command.startswith(application + '/Contents/'):
            if '/Contents/MacOS/' in command or '/Resources/codex' in command:
                raise OSError('Desktop host worker is still running')
    databases, files = _storage_files(home)
    if databases != expected_databases:
        raise OSError('History database changed during shutdown verification')
    if files:
        handles = subprocess.run(['/usr/sbin/lsof', '-t', '--', *files],
                                 capture_output=True, text=True, timeout=20)
        after_databases, after_files = _storage_files(home)
        if (after_databases != databases or
                any(after_files.get(path) != files[path] for path in databases)):
            raise OSError('History database changed during shutdown verification')
        if after_files != files:
            return False
        if handles.returncode not in (0, 1) or handles.stderr.strip():
            raise OSError('Cannot verify history storage handles')
        try:
            holders = {int(value) for value in handles.stdout.split()}
        except ValueError as error:
            raise OSError('Unexpected history handle inventory') from error
        if holders - allowed:
            raise OSError('Another process still has history storage open')
    return True


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
                "detail": "目前仅 macOS 实现自动重启；请手动重新打开宿主"}
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
