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
    except (ImportError, AttributeError):
        # AttributeError：非 Windows 平台上 ctypes.WinDLL 根本不存在
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
        _sync_vault(provider_id, secret)
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
        _sync_vault(provider_id, secret)
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
    _sync_vault(provider_id, secret)


def _sync_vault(provider_id: str, secret: str) -> None:
    """顺手把密钥存进加密保险库。

    钥匙串可能因为系统更新、磁盘写满、清理工具一夜丢光条目（实测事故：
    2026-09-20 登录钥匙串夜间被锁/重置，四个 key 全没了）。保险库是自动
    恢复的依据，必须在每次成功写入时同步。失败绝不抛错 —— 它只是副本。
    """
    try:
        from . import vault
        vault.upsert(provider_id, secret)
    except Exception:  # noqa: BLE001
        pass


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
        service = SERVICE_PREFIX + provider_id
        result = _run([
            "/usr/bin/security", "find-generic-password",
            "-a", _account(),
            "-s", service,
            "-w",
        ])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # 用户名对不上就找不到了：改过账户名、或用 sudo 跑过一次，
        # 条目还在但 acct 不是当前用户。按服务名再找一次，别直接判失败。
        fallback = _run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"])
        if fallback.returncode == 0 and fallback.stdout.strip():
            return fallback.stdout.strip()
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
    # 用户明确删除密钥时保险库也要删，否则会被自动恢复救回来
    try:
        from . import vault
        vault.remove(provider_id)
    except Exception:  # noqa: BLE001
        pass
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


def helper_python() -> str:
    """凭据助手用哪个解释器：必须是**探测过、真跑通过**的绝对路径。

    以前这里写死 `#!/usr/bin/env python3`，把解释器交给 PATH 去解析 ——
    真机上出过事故：某台 Mac 上 PATH 里第一个 python3 是个 python.org 装的
    3.7，太老，dyld 连 CoreFoundation 都加载不了，进程直接 SIGABRT
    （连 Python 都没起来，报错里能看到 dyld cache not loaded）。
    于是 Codex 取不到密钥，**所有第三方模型全用不了**；而图形界面却正常，
    因为 launch.sh 启动时是探测过解释器的 —— 一半正常一半崩，最难排查。

    所以这里改成写死一个当场验证过的解释器：
      · 先试当前正在跑本工具的这个（它此刻刚刚跑通了我们的代码，必然可用）
      · 再退到几个常见位置，逐个**真跑一次**，不通就换下一个
      · 全都不行才退回 env python3（至少保持和以前一样的行为）
    判断标准是"能不能真的执行"，不是"版本号看起来够不够" —— 上面那个 3.7
    版本号也能读到，但它根本启动不了。
    """
    import subprocess

    probe = (
        "import sys;"
        "raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"
    )
    candidates = [
        sys.executable,
        "/usr/bin/python3",
        "/usr/local/bin/python3",
        "/opt/homebrew/bin/python3",
    ]
    for candidate in candidates:
        if not candidate or not os.path.isabs(candidate):
            continue
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", probe], capture_output=True, timeout=15)
        except (subprocess.SubprocessError, OSError):
            continue
        if result.returncode == 0:
            return candidate
    return "/usr/bin/env python3"


# 这个脚本是 Codex 每次请求时执行的，必须自己找到本工具的运行时，
# 所以把路径写死进去；三个平台共用同一份逻辑（macOS 钥匙串 /
# Linux Secret Service / Windows DPAPI 由 secrets.py 自己判断）。
HELPER_TEMPLATE = '''#!{python}
"""Codex 凭据助手：只把密钥写到 stdout，其它信息一律走 stderr。

由 ChatGPT Model Switcher 生成，删除后重新运行 init 即可恢复。
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
        secret = None
        print("credential unavailable: %s" % error, file=sys.stderr)
    if not secret:
        # 钥匙串读不到时从加密保险库取，并顺手写回钥匙串。
        # 实测钥匙串可能一夜丢光条目（系统更新/磁盘写满/清理工具），
        # 没有这道兜底，Codex 的请求和每日自动化会一起断。
        try:
            from codex_switcher import vault
            secret = vault.get(provider)
            if secret:
                try:
                    secrets.store(provider, secret)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
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
    return HELPER_TEMPLATE.format(python=helper_python(),
                                  runtime=str(runtime_root()))


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
