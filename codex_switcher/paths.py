"""路径解析。

所有路径都在运行时基于当前用户的 HOME 计算，仓库里不保存任何机器专属路径。
"""

from __future__ import annotations

import os
from pathlib import Path


def codex_home() -> Path:
    """Codex 主目录，遵循 CODEX_HOME 环境变量。"""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def state_dir() -> Path:
    return codex_home() / "model-switcher"


def catalog_dir() -> Path:
    return state_dir() / "catalogs"


def backups_dir() -> Path:
    return state_dir() / "backups"


def credentials_dir() -> Path:
    """仅在系统钥匙串不可用时使用的回退目录（0600 权限）。"""
    return state_dir() / "credentials"


def state_file() -> Path:
    return state_dir() / "providers.json"


def lock_file() -> Path:
    return state_dir() / "switch.lock"


def config_path() -> Path:
    return codex_home() / "config.toml"


def bin_dir() -> Path:
    return codex_home() / "bin"


def keychain_helper() -> Path:
    """Codex 调用的凭据助手脚本路径。"""
    return bin_dir() / "codex-provider-keychain.py"


def sessions_dir() -> Path:
    return codex_home() / "sessions"


def gui_state_file() -> Path:
    """图形界面运行时写下的端口与进程号，供 `codex-switcher stop` 使用。"""
    return state_dir() / "gui.json"


def bridge_pid_file() -> Path:
    return state_dir() / "bridge.pid"


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path
