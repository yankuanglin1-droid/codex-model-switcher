"""核心动作：添加平台、拉模型、切换、恢复官方。

所有写配置的动作都集中在这里，方便审计：读了什么、写了什么、备份在哪。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import balance as balance_module
from . import bridge as bridge_module
from . import catalog as catalog_module
from . import configfile, paths, registry, secrets, state as state_module
from .discovery import DiscoveryError, fetch_models, probe_responses, rank_models


class SwitchError(RuntimeError):
    pass


OFFICIAL_PROVIDER = "openai"
DEFAULT_OFFICIAL_MODEL = "gpt-5-codex"


def slugify(value: str) -> str:
    """把用户填的平台名转成安全的 provider id。"""
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        text = "provider"
    if not text[0].isalpha():
        text = "p-" + text
    return text[:40]


def unique_id(state: Dict, base: str) -> str:
    existing = set(state_module.provider_ids(state))
    if base in registry.RESERVED_PROVIDER_IDS:
        base = base + "-local"
    if base not in existing:
        return base
    index = 2
    while "%s-%d" % (base, index) in existing:
        index += 1
    return "%s-%d" % (base, index)


def effective_base_url(record: Dict) -> str:
    """Codex 实际要连的地址：原生平台直连，其余走本地协议桥。"""
    if record.get("transport") == "native":
        return (record.get("base_url") or "").rstrip("/")
    port = int(record.get("bridge_port") or bridge_module.DEFAULT_PORT)
    return "http://127.0.0.1:%d/%s/v1" % (port, record.get("id"))


def provider_settings(record: Dict, model_id: str) -> Dict:
    """写进 config.toml 顶部的模型相关字段。"""
    override = (record.get("model_overrides") or {}).get(model_id) or {}
    effort = override.get("default_reasoning_level") or record.get("default_reasoning_effort") or "high"
    settings = {
        "model_provider": record["id"],
        "model": model_id,
        "model_reasoning_effort": effort,
        "model_reasoning_summary": "none",
        "model_supports_reasoning_summaries": True,
        "model_catalog_json": str(catalog_module.catalog_path(record["id"])),
    }
    context = override.get("context_window")
    if context:
        settings["model_context_window"] = int(context)
    return settings


def official_settings(model_id: str) -> Dict:
    return {
        "model_provider": OFFICIAL_PROVIDER,
        "model": model_id or DEFAULT_OFFICIAL_MODEL,
    }


def build_provider_record(
    *,
    provider_id: str,
    label: str,
    base_url: str,
    models_url: str,
    transport: str = "auto",
    preset_id: Optional[str] = None,
    balance: Optional[Dict] = None,
    console_url: Optional[str] = None,
    requires_key: bool = True,
    notes: Optional[str] = None,
    extra_headers: Optional[Dict] = None,
    default_reasoning_effort: str = "high",
) -> Dict:
    return {
        "id": provider_id,
        "label": label,
        "preset_id": preset_id,
        "base_url": (base_url or "").strip().rstrip("/"),
        "upstream_base_url": (base_url or "").strip().rstrip("/"),
        "models_url": (models_url or "").strip(),
        "wire_api": registry.WIRE_API,
        "transport": transport if transport in ("native", "bridge") else "auto",
        "bridge_port": bridge_module.DEFAULT_PORT,
        "balance": balance,
        "console_url": console_url,
        "requires_key": bool(requires_key),
        "notes": notes,
        "extra_headers": extra_headers or {},
        "default_reasoning_effort": default_reasoning_effort,
        "models": {},
        "model_overrides": {},
    }


def discover(record: Dict, api_key: Optional[str]) -> List[str]:
    """拉取模型清单；拉不到就抛错，由调用方决定是否改用手动模式。"""
    url = record.get("models_url") or registry.derive_models_url(record.get("base_url", ""))
    if not url:
        raise DiscoveryError("没有可用的模型清单地址")
    model_ids, _ = fetch_models(url, api_key, extra_headers=record.get("extra_headers") or {})
    if not model_ids:
        raise DiscoveryError("平台返回了空的模型列表，请改用手动添加模型")
    return rank_models(model_ids)


def save_record(state: Dict, record: Dict) -> None:
    state_module.upsert_provider(state, record)
    state_module.save(state)
    catalog_module.write_catalog(record["id"], record, list((record.get("models") or {}).keys()))


def add_provider(
    *,
    provider_id: Optional[str] = None,
    label: str,
    base_url: str,
    models_url: Optional[str] = None,
    transport: str = "auto",
    api_key: Optional[str] = None,
    preset_id: Optional[str] = None,
    balance: Optional[Dict] = None,
    console_url: Optional[str] = None,
    requires_key: bool = True,
    notes: Optional[str] = None,
    extra_headers: Optional[Dict] = None,
    manual_models: Optional[List[str]] = None,
    auto_discover: bool = True,
    switch_now: bool = False,
    default_model: Optional[str] = None,
    default_reasoning_effort: str = "high",
) -> Dict:
    """添加一个平台。返回 {record, models, discovery_error}。"""
    state = state_module.load()
    identifier = unique_id(state, provider_id or slugify(label or preset_id or "provider"))
    if identifier == OFFICIAL_PROVIDER:
        identifier = "provider-" + identifier

    record = build_provider_record(
        provider_id=identifier,
        label=label or identifier,
        base_url=base_url,
        models_url=models_url or registry.derive_models_url(base_url),
        transport=transport,
        preset_id=preset_id,
        balance=balance,
        console_url=console_url,
        requires_key=requires_key,
        notes=notes,
        extra_headers=extra_headers,
        default_reasoning_effort=default_reasoning_effort,
    )

    if requires_key and api_key:
        secrets.store(identifier, api_key)

    # 自动探测平台是否自带 Responses 接口
    if record.get("transport") == "auto":
        probe_key = api_key or (secrets.load(identifier) if requires_key else None)
        preset_transport = (registry.preset(preset_id) or {}).get("transport")
        probe = probe_responses(record["base_url"], probe_key)
        if probe["transport"] == "native":
            record["transport"] = "native"
        elif probe["transport"] == "bridge":
            record["transport"] = "bridge"
        elif preset_transport in ("native", "bridge"):
            record["transport"] = preset_transport
        else:
            record["transport"] = "bridge"
        record["transport_evidence"] = probe.get("evidence")

    discovery_error = None
    model_ids: List[str] = []
    if auto_discover and record["models_url"]:
        try:
            model_ids = discover(record, api_key or (secrets.load(identifier) if requires_key else None))
        except DiscoveryError as exc:
            discovery_error = str(exc)
    for item in manual_models or []:
        item = item.strip()
        if item and item not in model_ids:
            model_ids.append(item)
    if not model_ids:
        model_ids = list(manual_models or [])

    if model_ids:
        state_module.sync_models(record, model_ids)
    else:
        record["models"] = {}

    state_module.upsert_provider(state, record)
    state_module.save(state)
    catalog_module.write_catalog(identifier, record, list((record.get("models") or {}).keys()))

    if switch_now:
        chosen = default_model or (model_ids[0] if model_ids else None)
        if chosen:
            switch_to(identifier, chosen)
            record = state_module.get_provider(state_module.load(), identifier) or record

    return {"record": record, "models": model_ids, "discovery_error": discovery_error}


def refresh_models(provider_id: str) -> Dict:
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    api_key = secrets.load(provider_id) if record.get("requires_key", True) else None
    try:
        model_ids = discover(record, api_key)
    except DiscoveryError as exc:
        raise SwitchError(str(exc))
    state_module.sync_models(record, model_ids)
    state_module.save(state)
    catalog_module.write_catalog(provider_id, record, list((record.get("models") or {}).keys()))
    return {"record": record, "models": model_ids}


def switch_to(provider_id: str, model_id: Optional[str] = None, dry_run: bool = False) -> Dict:
    """切换默认平台与型号。旧任务不受影响。"""
    state = state_module.load()
    config = paths.config_path()
    if provider_id == OFFICIAL_PROVIDER:
        if not config.exists():
            raise SwitchError("找不到 Codex 配置文件：%s" % config)
        text = config.read_text()
        settings = official_settings(model_id or DEFAULT_OFFICIAL_MODEL)
        new_text = configfile.rewrite_model_settings(text, settings)
        if dry_run:
            return {"provider": OFFICIAL_PROVIDER, "model": settings["model"], "dry_run": True}
        backup = configfile.backup(config)
        configfile.atomic_write(config, new_text, text)
        return {"provider": OFFICIAL_PROVIDER, "model": settings["model"], "backup": str(backup)}

    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)

    models = list((record.get("models") or {}).keys())
    if not models:
        raise SwitchError("该平台还没有模型，请先运行 refresh 或手动添加模型")
    chosen = model_id or record.get("default_model") or models[0]
    if chosen not in models:
        raise SwitchError("该平台没有名为 %s 的模型" % chosen)

    if record.get("requires_key", True) and not secrets.load(provider_id):
        raise SwitchError("该平台的密钥不在系统钥匙串里，请重新添加平台或补充密钥")

    catalog_target = catalog_module.catalog_path(provider_id)
    if not catalog_target.exists():
        catalog_module.write_catalog(provider_id, record, models)

    fields = {"name": record.get("config_name") or record["label"],
              "base_url": effective_base_url(record),
              "wire_api": registry.WIRE_API}
    auth = None
    if record.get("requires_key", True):
        helper = secrets.install_helper()
        auth = {"command": str(helper), "args": [provider_id], "timeout_ms": 10000,
                "refresh_interval_ms": 300000}

    settings = provider_settings(record, chosen)

    paths.ensure_dir(paths.state_dir())
    lock_path = paths.lock_file()
    with lock_path.open("a+") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not config.exists():
            raise SwitchError("找不到 Codex 配置文件：%s" % config)
        text = config.read_text()
        try:
            with_block = configfile.upsert_provider_block(text, provider_id, fields, auth)
            new_text = configfile.rewrite_model_settings(with_block, settings)
        except configfile.ConfigError as exc:
            raise SwitchError("写入配置失败：%s" % exc)
        if dry_run:
            return {"provider": provider_id, "model": chosen, "dry_run": True}
        backup = configfile.backup(config)
        configfile.atomic_write(config, new_text, text)

    record["default_model"] = chosen
    state_module.upsert_provider(state, record)
    state_module.save(state)
    return {"provider": provider_id, "label": record["label"], "model": chosen, "backup": str(backup)}


def remove_provider(provider_id: str, purge_key: bool = True) -> Dict:
    state = state_module.load()
    if not state_module.get_provider(state, provider_id):
        raise SwitchError("没有找到平台：%s" % provider_id)
    config = paths.config_path()
    backup = None
    if config.exists():
        text = config.read_text()
        try:
            parse_check = configfile.read_top_level(text, ("model_provider",)).get("model_provider")
        except Exception:  # noqa: BLE001
            parse_check = None
        new_text = configfile.remove_provider_block(text, provider_id)
        if new_text != text:
            if parse_check == provider_id:
                new_text = configfile.rewrite_model_settings(new_text, official_settings(DEFAULT_OFFICIAL_MODEL))
            backup = configfile.backup(config)
            configfile.atomic_write(config, new_text, text)
    catalog_target = catalog_module.catalog_path(provider_id)
    if catalog_target.exists():
        catalog_target.unlink()
    if purge_key:
        secrets.delete(provider_id)
    state_module.remove_provider(state, provider_id)
    state_module.save(state)
    return {"provider": provider_id, "backup": str(backup) if backup else None, "key_purged": purge_key}


def current_status() -> Dict:
    config = paths.config_path()
    status = {"config_path": str(config), "exists": config.exists()}
    if not config.exists():
        return status
    try:
        document = configfile.read_top_level(
            config.read_text(),
            ("model_provider", "model", "model_catalog_json", "model_reasoning_effort"),
        )
    except Exception as exc:  # noqa: BLE001
        status["error"] = "配置文件解析失败：%s" % exc
        return status
    status["model_provider"] = document.get("model_provider", OFFICIAL_PROVIDER)
    status["model"] = document.get("model")
    status["catalog"] = document.get("model_catalog_json")
    return status


def provider_overview(include_balance: bool = False) -> List[Dict]:
    state = state_module.load()
    current = current_status().get("model_provider")
    bridge_cache: Dict[int, bool] = {}
    overview = []
    for provider_id, record in (state.get("providers") or {}).items():
        transport = record.get("transport")
        port = int(record.get("bridge_port") or bridge_module.DEFAULT_PORT)
        if transport != "native" and port not in bridge_cache:
            bridge_cache[port] = bridge_module.is_running(port)
        item = {
            "id": provider_id,
            "label": record.get("label"),
            "base_url": record.get("base_url"),
            "upstream_base_url": record.get("upstream_base_url") or record.get("base_url"),
            "effective_base_url": effective_base_url(record),
            "transport": transport,
            "wire_api": registry.WIRE_API,
            "bridge_running": None if transport == "native" else bridge_cache.get(port, False),
            "models": list((record.get("models") or {}).keys()),
            "default_model": record.get("default_model"),
            "console_url": record.get("console_url"),
            "requires_key": record.get("requires_key", True),
            "has_key": secrets.exists(provider_id),
            "key_hint": secrets.mask(secrets.load(provider_id)) if secrets.exists(provider_id) else "未配置",
            "is_current": provider_id == current,
            "notes": record.get("notes"),
            "models_synced_at": record.get("models_synced_at"),
        }
        if include_balance:
            item["balance"] = balance_module.query(record, secrets.load(provider_id))
        overview.append(item)
    return overview
