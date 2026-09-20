"""本机加密保险库：钥匙串的自动恢复副本。

为什么要有它：钥匙串可能因为系统更新、磁盘写满、清理工具、误操作等原因
丢条目（2026-09-20 实测：登录钥匙串夜间被锁/重置，四个平台的 key 一夜
全没了，Codex 全线瘫痪）。钥匙串是唯一副本的话，丢了就只能翻明文文件。

保险库在**每次写入钥匙串时同步**存一份加密副本，恢复路径有三道：
  1. 凭据助手（Codex 每次请求都跑）：钥匙串读不到 → 直接从保险库取，
     顺手写回钥匙串 —— 请求和自动化根本不会断
  2. 链路检查（check / 界面横幅）：发现钥匙串缺 key → 自动恢复并如实报告
  3. 手动：codex-switcher vault --restore

加密设计（只防"顺手翻文件"，不防拿到磁盘的攻击者）：
  · 密钥文件（32 字节随机，0600）放在 ~/Library/Application Support，
    和保险库文件分处两处 —— 单删其中一个目录不会两败俱伤
  · AES-256-CBC，IV 每次随机，密钥经 PBKDF2(200k) 派生，走系统 openssl
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from . import paths

MAGIC = "CSVAULT1"
PBKDF2_ITERATIONS = 200_000


def key_file() -> Path:
    """保险库的加密密钥文件。刻意和保险库文件放在不同目录。"""
    override = os.environ.get("CODEX_SWITCHER_VAULT_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "codex-switcher"
    else:
        base = Path.home() / ".local" / "share" / "codex-switcher"
    return base / "vault.key"


def vault_file() -> Path:
    return paths.state_dir() / "vault.enc"


def _ensure_key() -> Optional[bytes]:
    path = key_file()
    try:
        if path.exists():
            raw = path.read_text().strip()
            key = bytes.fromhex(raw)
            if len(key) == 32:
                return key
            return None                      # 密钥文件坏了，宁可不解密也别猜
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        path.write_text(key.hex() + "\n", encoding="ascii")
        os.chmod(path, 0o600)
        return key
    except OSError:
        return None


def _derive(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", key, salt, PBKDF2_ITERATIONS)


def _encrypt(plaintext: str, key: bytes) -> Optional[str]:
    iv = os.urandom(16)
    derived = _derive(key, iv)               # IV 兼作盐：每次保存都换派生密钥
    result = subprocess.run(
        ["/usr/bin/openssl", "enc", "-aes-256-cbc", "-K", derived.hex(),
         "-iv", iv.hex()],
        input=plaintext.encode("utf-8"), capture_output=True, timeout=30)
    if result.returncode != 0:
        return None
    return MAGIC + ":" + base64.b64encode(iv + result.stdout).decode("ascii")


def _decrypt(payload: str, key: bytes) -> Optional[Dict]:
    try:
        magic, encoded = payload.strip().split(":", 1)
        if magic != MAGIC:
            return None
        blob = base64.b64decode(encoded)
        iv, ciphertext = blob[:16], blob[16:]
        derived = _derive(key, iv)
        result = subprocess.run(
            ["/usr/bin/openssl", "enc", "-d", "-aes-256-cbc", "-K", derived.hex(),
             "-iv", iv.hex()],
            input=ciphertext, capture_output=True, timeout=30)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 保险库坏了解不开就当没有，绝不能让调用方崩
        return None


def load_all() -> Dict:
    """读出保险库里的全部密钥。解不开返回空 dict，绝不抛错。"""
    key = _ensure_key()
    if key is None:
        return {}
    try:
        payload = vault_file().read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {}
    return _decrypt(payload, key) or {}


def _save_all(data: Dict) -> bool:
    key = _ensure_key()
    if key is None:
        return False
    encrypted = _encrypt(json.dumps(data, ensure_ascii=False), key)
    if not encrypted:
        return False
    try:
        path = vault_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encrypted + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return True
    except OSError:
        return False


def get(provider_id: str) -> Optional[str]:
    return load_all().get(provider_id) or None


def upsert(provider_id: str, secret: str) -> bool:
    """存一份。失败不影响主流程（调用方都不该因保险库报错）。"""
    if not secret:
        return False
    data = load_all()
    if data.get(provider_id) == secret:
        return True
    data[provider_id] = secret
    return _save_all(data)


def remove(provider_id: str) -> bool:
    data = load_all()
    if provider_id not in data:
        return True
    del data[provider_id]
    return _save_all(data)


def restore_all() -> Dict:
    """把保险库里的密钥写回钥匙串。返回 {provider: 恢复与否}。"""
    from . import secrets
    result = {}
    for provider_id, secret in load_all().items():
        try:
            secrets.store(provider_id, secret)
            result[provider_id] = secrets.load(provider_id) == secret
        except Exception:  # noqa: BLE001
            result[provider_id] = False
    return result


def sync_from_keychain() -> Dict:
    """把钥匙串里已有、但保险库里没有的密钥补进保险库。

    部署后跑一次即可给存量密钥建副本；之后每次 store() 都会自动同步。
    """
    from . import secrets
    data = load_all()
    synced, checked = [], 0
    try:
        from . import state as state_module
        providers_state = state_module.load().get("providers") or {}
        if isinstance(providers_state, dict):
            providers = list(providers_state.keys())
        else:                                # 旧格式是列表
            providers = [p.get("id") for p in providers_state
                         if isinstance(p, dict) and p.get("id")]
    except Exception:  # noqa: BLE001
        providers = []
    for provider_id in providers:
        checked += 1
        if data.get(provider_id):
            continue
        key = secrets.load(provider_id)
        if key and upsert(provider_id, key):
            synced.append(provider_id)
    return {"checked": checked, "synced": synced}
