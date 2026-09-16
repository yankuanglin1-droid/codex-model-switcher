"""凭据存储：优先系统钥匙串，最后才用 0600 文件回退。

任何函数都不会打印、返回或记录日志化的密钥。
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import paths

SERVICE_PREFIX = "com.codex.model-switcher."

# 读钥匙串要起一次子进程（几十毫秒）。界面每次刷新都读一遍太浪费，加一个很短的
# 缓存；改完密钥最多 20 秒内会重新读取。
_CACHE_TTL_SECONDS = 20
_CACHE: dict = {}


def _account() -> str:
    return getpass.getuser()


def _run(cmd: list, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=20)


def _macos_available() -> bool:
    return sys.platform == "darwin" and Path("/usr/bin/security").exists()


def _linux_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def backend() -> str:
    if _macos_available():
        return "keychain"
    if _linux_available():
        return "secret-service"
    return "file"


def backend_label() -> str:
    return {
        "keychain": "macOS 钥匙串",
        "secret-service": "Linux Secret Service",
        "file": "本地文件（0600，保护最弱）",
    }[backend()]


def _fallback_file(provider_id: str) -> Path:
    return paths.credentials_dir() / (provider_id + ".key")


def store(provider_id: str, secret: str) -> None:
    """写入或更新密钥。"""
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("密钥为空")
    _CACHE.pop(provider_id, None)
    if _macos_available():
        result = _run([
            "/usr/bin/security", "add-generic-password",
            "-a", _account(),
            "-s", SERVICE_PREFIX + provider_id,
            "-w", secret,
            "-U",
        ])
        if result.returncode != 0:
            raise RuntimeError("写入 macOS 钥匙串失败：" + (result.stderr or "").strip()[:200])
        return
    if _linux_available():
        result = _run([
            "secret-tool", "store",
            "--label", "Codex Model Switcher: " + provider_id,
            "service", SERVICE_PREFIX + provider_id,
            "account", _account(),
        ], stdin=secret)
        if result.returncode != 0:
            raise RuntimeError("写入 Secret Service 失败：" + (result.stderr or "").strip()[:200])
        return
    target = _fallback_file(provider_id)
    paths.ensure_dir(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secret)


def load(provider_id: str) -> Optional[str]:
    """读取密钥；不存在时返回 None。"""
    import time
    cached = _CACHE.get(provider_id)
    if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    value = _load_uncached(provider_id)
    _CACHE[provider_id] = (time.time(), value)
    return value


def _load_uncached(provider_id: str) -> Optional[str]:
    if _macos_available():
        result = _run([
            "/usr/bin/security", "find-generic-password",
            "-a", _account(),
            "-s", SERVICE_PREFIX + provider_id,
            "-w",
        ])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    if _linux_available():
        result = _run([
            "secret-tool", "lookup",
            "service", SERVICE_PREFIX + provider_id,
            "account", _account(),
        ])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    target = _fallback_file(provider_id)
    if target.exists():
        value = target.read_text().strip()
        return value or None
    return None


def delete(provider_id: str) -> bool:
    _CACHE.pop(provider_id, None)
    removed = False
    if _macos_available():
        result = _run([
            "/usr/bin/security", "delete-generic-password",
            "-a", _account(),
            "-s", SERVICE_PREFIX + provider_id,
        ])
        removed = removed or result.returncode == 0
    if _linux_available():
        result = _run([
            "secret-tool", "clear",
            "service", SERVICE_PREFIX + provider_id,
            "account", _account(),
        ])
        removed = removed or result.returncode == 0
    target = _fallback_file(provider_id)
    if target.exists():
        target.unlink()
        removed = True
    return removed


def exists(provider_id: str) -> bool:
    return load(provider_id) is not None


def mask(secret: Optional[str]) -> str:
    """给界面或日志用的脱敏显示。永远不返回完整密钥。"""
    if not secret:
        return "未配置"
    if len(secret) <= 10:
        return secret[:2] + "•" * 6
    return secret[:6] + "…" + secret[-4:]


HELPER_SOURCE = '''#!/usr/bin/env python3
"""Codex 凭据助手：只把密钥写到 stdout，其它信息一律走 stderr。

此文件由 Codex Model Switcher 生成，可安全删除后重新生成。
"""
import getpass
import subprocess
import sys

SERVICE_PREFIX = "com.codex.model-switcher."


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: codex-provider-keychain.py <provider-id>", file=sys.stderr)
        return 2
    provider = sys.argv[1].strip()
    command = ["/usr/bin/security", "find-generic-password",
               "-a", getpass.getuser(), "-s", SERVICE_PREFIX + provider, "-w"]
    result = subprocess.run(command, capture_output=True, text=True)
    secret = (result.stdout or "").strip()
    if result.returncode != 0 or not secret:
        print("credential unavailable for provider: " + provider, file=sys.stderr)
        return 1
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_helper() -> Path:
    """把 Codex 调用的凭据助手写入 Codex bin 目录并赋予可执行权限。"""
    target = paths.keychain_helper()
    paths.ensure_dir(target.parent)
    if not (target.exists() and target.read_text() == HELPER_SOURCE):
        target.write_text(HELPER_SOURCE)
    os.chmod(target, 0o700)
    return target
