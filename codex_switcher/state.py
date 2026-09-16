"""本工具的本地状态文件（~/.codex/model-switcher/providers.json）。

只存“哪个平台、地址、有哪些模型”，不存密钥。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import paths
from . import platform_compat

SCHEMA_VERSION = 3


def default_state() -> Dict:
    return {"schema_version": SCHEMA_VERSION, "providers": {}}


def load() -> Dict:
    target = paths.state_file()
    if not target.exists():
        return default_state()
    try:
        state = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return default_state()
    if not isinstance(state, dict) or "providers" not in state:
        return default_state()
    state.setdefault("schema_version", SCHEMA_VERSION)
    return state


def save(state: Dict) -> None:
    target = paths.state_file()
    paths.ensure_dir(target.parent)
    fd, temp = tempfile.mkstemp(prefix="." + target.name + ".", dir=str(target.parent))
    try:
        platform_compat.chmod_private_fd(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def provider_ids(state: Dict) -> List[str]:
    return list(state.get("providers", {}).keys())


def get_provider(state: Dict, provider_id: str) -> Optional[Dict]:
    return (state.get("providers") or {}).get(provider_id)


def upsert_provider(state: Dict, record: Dict) -> Dict:
    providers = state.setdefault("providers", {})
    identifier = record.get("id")
    if not identifier:
        raise ValueError("平台记录缺少 id 字段，无法保存")
    existing = providers.get(identifier)
    if existing:
        merged = dict(existing)
        merged.update(record)
        record = merged
    record.setdefault("added_at", datetime.datetime.now().isoformat(timespec="seconds"))
    providers[identifier] = record
    return record


def remove_provider(state: Dict, provider_id: str) -> bool:
    return (state.get("providers") or {}).pop(provider_id, None) is not None


def sync_models(record: Dict, model_ids: List[str]) -> Dict:
    """把新拉到的模型清单合并进记录，保留用户已有的手动设置。"""
    models = record.get("models") or {}
    merged = {}
    for model_id in model_ids:
        if model_id in models:
            merged[model_id] = models[model_id]
        else:
            merged[model_id] = {"added_at": datetime.datetime.now().isoformat(timespec="seconds")}
    # 用户手动加过、但平台这次没返回的模型保留下来
    for model_id, payload in models.items():
        if model_id not in merged and payload.get("manual"):
            merged[model_id] = payload
    record["models"] = merged
    record["models_synced_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return record
