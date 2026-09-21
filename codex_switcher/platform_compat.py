"""跨平台的小工具。

Windows 上没有 fcntl、没有 os.fchmod、也不能对目录做 fsync，
这些都在这里兜住，其余模块只调用本文件提供的接口。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 官方支持 Python 3.9+：3.11+ 有内置 tomllib，3.9/3.10 走降级校验
MIN_PYTHON = (3, 9)
PREFERRED_PYTHON = (3, 11)


def python_ok(version_info=None) -> bool:
    info = version_info or sys.version_info
    return (info[0], info[1]) >= MIN_PYTHON


def chmod_private_fd(fd: int, mode: int = 0o600) -> None:
    """给文件描述符设权限。Windows 没有 fchmod，忽略即可。"""
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass


def chmod_private(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def fsync_directory(path: Path) -> None:
    """把目录项落盘。Windows 不允许 open 目录，直接跳过。"""
    if IS_WINDOWS:
        return
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """进程间文件锁：POSIX 用 fcntl，Windows 用 msvcrt。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    chmod_private(path, 0o600)
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            # 用 LK_NBLCK 自己重试，而不是 LK_LOCK：LK_LOCK 只会重试 10 次
            # 就抛异常，界面、协议桥、命令行同时抢锁时可能刚好撞上。
            deadline = time.monotonic() + 60
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def process_command_line(pid: int) -> str:
    """尽量拿到某个进程的命令行；拿不到就返回空串。"""
    if pid <= 1:
        return ""
    if IS_WINDOWS:
        # Verify the command line, not merely the shared python.exe image name.
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                 "(Get-CimInstance Win32_Process -Filter 'ProcessId = %d').CommandLine" % pid],
                capture_output=True, text=True, encoding="utf-8", timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return (result.stdout or "").strip() if result.returncode == 0 else ""
        except Exception:
            return ""
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return ""
    return (result.stdout or "").strip()


def is_our_process(pid: int, marker: str = "codex_switcher") -> bool:
    """判断这个 PID 是不是本工具起的，避免误杀复用了同一 PID 的其它程序。"""
    command = process_command_line(pid)
    if not command:
        return False
    return marker in command or (IS_WINDOWS and "packaging/windows/app.py" in command.replace("\\", "/"))


def spawn_detached(command: list, log_path: Optional[Path] = None) -> int:
    """后台起一个进程，不占住当前终端、也不弹控制台窗口。"""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "ab")
        kwargs["stdout"] = stream
        kwargs["stderr"] = stream
        kwargs["stdin"] = subprocess.DEVNULL
    process = subprocess.Popen(command, **kwargs)  # noqa: S603
    return process.pid


def default_state_hint() -> str:
    if IS_WINDOWS:
        return "%LOCALAPPDATA%\\codex-switcher"
    return "~/.local/share/codex-switcher"


def runtime_dir() -> Path:
    """安装后的运行时目录（凭据助手要把它写死进去）。"""
    override = os.environ.get("CODEX_SWITCHER_HOME")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "codex-switcher"
    return Path.home() / ".local" / "share" / "codex-switcher"


def resolve_runtime_root(fallback: Path) -> Path:
    """挑一个稳定的运行时根目录。

    优先用安装目录：从仓库里执行 `python -m codex_switcher` 时，Python 会把
    当前目录排在 sys.path 最前面，于是 __file__ 指向仓库。要是把这个路径写进
    凭据助手，仓库一挪走 Codex 就取不到密钥了。
    """
    candidates = [runtime_dir(), fallback]
    for candidate in candidates:
        try:
            if (candidate / "codex_switcher" / "__init__.py").exists():
                return candidate
        except OSError:
            continue
    return fallback
