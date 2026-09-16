"""初始化与自检。"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from typing import Dict, Optional

from . import paths, secrets, state as state_module

AGENT_LABEL = "com.codex.model-switcher.bridge"


def codex_version() -> Optional[str]:
    for candidate in ("codex",):
        binary = shutil.which(candidate)
        if not binary:
            continue
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        text = (result.stdout or result.stderr or "").strip()
        if text:
            return text.splitlines()[0][:80]
    return None


def ensure_helper():
    return secrets.install_helper()


def bootstrap() -> Dict:
    """创建目录、装好凭据助手、准备状态文件。幂等，可重复运行。"""
    paths.ensure_dir(paths.state_dir())
    paths.ensure_dir(paths.catalog_dir())
    paths.ensure_dir(paths.backups_dir())
    helper = ensure_helper()
    if not paths.state_file().exists():
        state_module.save(state_module.default_state())
    return {
        "helper": str(helper),
        "secret_backend": secrets.backend_label(),
        "state_dir": str(paths.state_dir()),
        "codex_version": codex_version(),
    }


def agent_plist_path():
    return paths.Path.home() / "Library" / "LaunchAgents" / (AGENT_LABEL + ".plist")


def package_root():
    """codex_switcher 包所在的上一级目录，用于设置 PYTHONPATH。"""
    return paths.Path(__file__).resolve().parents[1]


def install_launch_agent(port: int = 8787):
    """macOS：把协议桥装成开机自启的后台服务。"""
    if sys.platform != "darwin":
        raise RuntimeError("后台服务目前只支持 macOS；其它系统请用 `codex-switcher bridge` 常驻运行。")
    log_dir = paths.ensure_dir(paths.state_dir())
    payload = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, "-m", "codex_switcher.bridge", "--port", str(port)],
        "EnvironmentVariables": {"PYTHONPATH": str(package_root())},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "bridge.log"),
        "StandardErrorPath": str(log_dir / "bridge.err.log"),
    }
    target = agent_plist_path()
    paths.ensure_dir(target.parent)
    with target.open("wb") as stream:
        plistlib.dump(payload, stream)
    os.chmod(target, 0o600)
    _launchctl(["bootout", "gui/%d" % os.getuid(), str(target)])
    result = _launchctl(["bootstrap", "gui/%d" % os.getuid(), str(target)])
    if result is None:
        _launchctl(["load", "-w", str(target)])
    return target


def remove_launch_agent() -> bool:
    target = agent_plist_path()
    if not target.exists():
        return False
    _launchctl(["bootout", "gui/%d" % os.getuid(), str(target)])
    _launchctl(["unload", "-w", str(target)])
    target.unlink()
    return True


def _launchctl(arguments):
    if not shutil.which("launchctl"):
        return None
    try:
        return subprocess.run(["launchctl"] + arguments, capture_output=True, text=True, timeout=25)
    except Exception:  # noqa: BLE001
        return None
