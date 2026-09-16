"""凭据存储：优先系统钥匙串，最后才用 0600 文件回退。

任何函数都不会打印、返回或记录日志化的密钥。
"""

from __future__ import annotations

import getpass
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import paths
from . import platform_compat

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
    if _dpapi_available():
        return "dpapi"
    return "file"


def backend_label() -> str:
    return {
        "keychain": "macOS 钥匙串",
        "secret-service": "Linux Secret Service",
        "dpapi": "Windows 凭据加密（DPAPI）",
        "file": "本地文件（0600，保护最弱）",
    }[backend()]


# ------------------------------------------------------- Windows 凭据加密

def _dpapi_available() -> bool:
    return platform_compat.IS_WINDOWS


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, unprotect: bool) -> Optional[bytes]:
    """调用 Windows DPAPI 加解密。密钥绑当前用户，别的账号解不开。"""
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except ImportError:
        return None
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    function = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    if not function(ctypes.byref(blob_in), None, None, None, None, flags,
                    ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


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
    if _dpapi_available():
        blob = _dpapi(secret.encode("utf-8"), unprotect=False)
        if blob is None:
            raise RuntimeError("Windows 凭据加密失败，无法安全保存密钥")
        target.write_bytes(b"DPAPI1\n" + blob)
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secret)
    platform_compat.chmod_private(target, 0o600)


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
        raw = target.read_bytes()
        if raw.startswith(b"DPAPI1\n"):
            plain = _dpapi(raw[len(b"DPAPI1\n"):], unprotect=True)
            if plain is None:
                return None
            text = plain.decode("utf-8", "replace").strip()
            return text or None
        value = raw.decode("utf-8", "replace").strip()
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


# 这个脚本是 Codex 每次请求时执行的，必须自己找到本工具的运行时，
# 所以把路径写死进去；三个平台共用同一份逻辑（macOS 钥匙串 /
# Linux Secret Service / Windows DPAPI 由 secrets.py 自己判断）。
HELPER_TEMPLATE = '''#!/usr/bin/env python3
"""Codex 凭据助手：只把密钥写到 stdout，其它信息一律走 stderr。

由 codex（ChatGPT App）多平台模型切换 生成，删除后重新运行 init 即可恢复。
"""
import sys

sys.path.insert(0, {runtime!r})

from codex_switcher import secrets  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: codex-provider-keychain.py <provider-id>", file=sys.stderr)
        return 2
    provider = sys.argv[1].strip()
    try:
        secret = secrets.load(provider)
    except Exception as error:  # noqa: BLE001
        print("credential unavailable: %s" % error, file=sys.stderr)
        return 1
    if not secret:
        print("credential unavailable for provider: " + provider, file=sys.stderr)
        return 1
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def runtime_root() -> Path:
    """本工具包所在的目录（用于写进凭据助手）。"""
    return platform_compat.resolve_runtime_root(Path(__file__).resolve().parents[1])


def helper_source() -> str:
    return HELPER_TEMPLATE.format(runtime=str(runtime_root()))


def install_helper() -> Path:
    """把 Codex 调用的凭据助手写入 Codex bin 目录。"""
    target = paths.keychain_helper()
    paths.ensure_dir(target.parent)
    source = helper_source()
    if not (target.exists() and target.read_text() == source):
        target.write_text(source)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def helper_command(provider_id: str):
    """返回 Codex 配置里该写的 (command, args)。

    Windows 上 .py 不是可执行文件，必须用解释器去跑它。
    """
    helper = install_helper()
    if platform_compat.IS_WINDOWS:
        return sys.executable, [str(helper), provider_id]
    return str(helper), [provider_id]
