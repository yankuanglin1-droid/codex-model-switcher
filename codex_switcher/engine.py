"""核心动作：添加平台、拉模型、切换、恢复官方。

所有写配置的动作都集中在这里，方便审计：读了什么、写了什么、备份在哪。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import balance as balance_module
from . import bridge as bridge_module
from . import capabilities as capabilities_module
from . import catalog as catalog_module
from . import contextguard as contextguard_module
from . import configfile, paths, registry, secrets, state as state_module
from . import platform_compat
from . import threads as threads_module
from .discovery import DiscoveryError, fetch_models, probe_responses, rank_models


class SwitchError(RuntimeError):
    pass


OFFICIAL_PROVIDER = "openai"
DEFAULT_OFFICIAL_MODEL = "gpt-5-codex"


def slugify(value: str) -> str:
    """把用户填的平台名转成安全的 provider id。"""
    original = (value or "").strip()
    text = original.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        # 全中文之类的名字：用短哈希保证唯一且稳定，比一律叫 provider 好
        text = "platform-" + hashlib.sha1(original.encode("utf-8")).hexdigest()[:6]
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
    if resolve_transport(record) == "native":
        return resolve_base_url(record)
    port = int(record.get("bridge_port") or bridge_module.DEFAULT_PORT)
    return "http://127.0.0.1:%d/%s/v1" % (port, record.get("id"))


def resolve_base_url(record: Dict, config_text: Optional[str] = None,
                     provider_id: Optional[str] = None) -> str:
    """确定平台的真实地址。

    顺序：状态文件里的字段 → 现有 config.toml 的 provider 块 → 内置预设。
    老版本状态文件不带 base_url，必须靠后两级兜底，否则会把地址写空。

    provider_id 最好由调用方显式传入：老记录里连 id 字段都没有。
    """
    for key in ("upstream_base_url", "base_url"):
        value = (record.get(key) or "").strip()
        if value:
            return value.rstrip("/")
    identifier = provider_id or record.get("id") or ""
    if config_text and identifier:
        found = configfile.provider_block_value(config_text, identifier, "base_url")
        if isinstance(found, str) and found.strip():
            return found.strip().rstrip("/")
    preset = registry.preset(record.get("preset_id") or "") or registry.preset(identifier)
    if preset and preset.get("base_url"):
        return preset["base_url"].rstrip("/")
    return ""


def resolve_transport(record: Dict) -> str:
    """判断一个平台该直连还是走协议桥。

    关键点：旧版本或手工建的状态文件里没有 transport 字段，那种记录本来就指向
    平台真实地址，必须按“直连”处理；否则会被误判成需要协议桥，
    把用户本来好用的配置改成指向 127.0.0.1。
    """
    return "bridge" if (record.get("transport") or "").strip() == "bridge" else "native"


def provider_settings(record: Dict, model_id: str, provider_id: Optional[str] = None) -> Dict:
    """写进 config.toml 顶部的模型相关字段。

    provider_id 由调用方显式传入：老状态记录里可能连 id 字段都没有。
    """
    identifier = provider_id or record.get("id") or ""
    if not identifier:
        raise SwitchError("这条记录缺少平台 ID，请重新添加该平台")
    override = (record.get("model_overrides") or {}).get(model_id) or {}
    effort = override.get("default_reasoning_level") or record.get("default_reasoning_effort") or "high"
    settings = {
        "model_provider": identifier,
        "model": model_id,
        "model_reasoning_effort": effort,
        "model_reasoning_summary": "none",
        "model_supports_reasoning_summaries": True,
        "model_catalog_json": str(catalog_module.catalog_path(identifier)),
    }
    context = override.get("context_window")
    if context:
        settings["model_context_window"] = int(context)

    # 反复压缩的根治：给 Codex 一个明确的、留有余量的压缩触发点。
    #
    # 不写这个键的时候，Codex 按「可用窗口的接近 100%」来判断要不要压。
    # 而压缩本身会往 rollout 里写一条约 1MB 的记录（其中 guardian_history
    # 单独 900KB），压缩产物本身就超过小窗口模型的可用窗口，
    # 于是「压完还是超、超限又压」。实测一个会话 17 分钟连压 199 次。
    #
    # 在窗口的 60% 处触发，压完的历史有地方落脚，压缩才是收敛的。
    # 详见 codex_switcher/contextguard.py 的模块说明。
    info = contextguard_module.model_window(catalog_module.catalog_path(identifier), model_id)
    effective = info["effective"] if info else None
    if not effective and context:
        effective = contextguard_module.effective_window(
            {"context_window": context,
             "effective_context_window_percent": catalog.DEFAULT_EFFECTIVE_PERCENT})
    if effective:
        settings["model_auto_compact_token_limit"] = contextguard_module.auto_compact_limit(effective)
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
    # 老记录可能缺 id / base_url / transport，切换时顺手补全，
    # 以后就不用再依赖 config.toml 反查了
    record.setdefault("id", provider_id)
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
    # 老记录可能缺 id / base_url / transport，顺手补全，
    # 以后就不用再依赖 config.toml 反查了
    record.setdefault("id", provider_id)

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

    auth = None
    if record.get("requires_key", True):
        command, arguments = secrets.helper_command(provider_id)
        auth = {"command": command, "args": arguments, "timeout_ms": 10000,
                "refresh_interval_ms": 300000}

    settings = provider_settings(record, chosen, provider_id)

    paths.ensure_dir(paths.state_dir())
    with platform_compat.file_lock(paths.lock_file()):
        if not config.exists():
            raise SwitchError("找不到 Codex 配置文件：%s" % config)
        text = config.read_text()
        # 地址要在这里确定：老状态文件不带 base_url，需要从现有配置里取回来
        upstream = resolve_base_url(record, text, provider_id)
        if not upstream:
            raise SwitchError(
                "这个平台没有记录 Base URL（多半是早期版本建的）。"
                "请重新添加一次：codex-switcher add --id %s --base-url <平台地址>" % provider_id)
        base_url = upstream if resolve_transport(record) == "native" else effective_base_url(record)
        fields = {"name": record.get("config_name") or record["label"],
                  "base_url": base_url,
                  "wire_api": registry.WIRE_API}
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
    record.setdefault("transport", "native")
    record["base_url"] = upstream
    record["upstream_base_url"] = upstream
    state_module.upsert_provider(state, record)
    state_module.save(state)

    # 顺手把「模型和服务商对不上」的旧任务修好，
    # 否则切完在旧对话里换模型还会报 model is not supported
    threads_fixed = 0
    try:
        threads_fixed = threads_module.repair().get("fixed", 0)
    except Exception:  # noqa: BLE001 - 修不动也不能影响切换本身
        threads_fixed = 0
    return {"provider": provider_id, "label": record["label"], "model": chosen,
            "backup": str(backup), "threads_fixed": threads_fixed}


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
    try:
        config_text = paths.config_path().read_text()
    except OSError:
        config_text = None
    bridge_cache: Dict[int, bool] = {}
    overview = []
    for provider_id, record in (state.get("providers") or {}).items():
        transport = resolve_transport(record)
        port = int(record.get("bridge_port") or bridge_module.DEFAULT_PORT)
        if transport == "bridge" and port not in bridge_cache:
            bridge_cache[port] = bridge_module.is_running(port)
        # 钥匙串读取要起子进程，每个平台只读一次（原来读了三遍）
        stored_key = secrets.load(provider_id)
        upstream = resolve_base_url(record, config_text, provider_id)
        effective = upstream if transport == "native" else "http://127.0.0.1:%d/%s/v1" % (port, provider_id)
        item = {
            "id": provider_id,
            "label": record.get("label"),
            "base_url": upstream or None,
            "upstream_base_url": upstream or None,
            "effective_base_url": effective,
            "transport": transport,
            "wire_api": registry.WIRE_API,
            "bridge_running": None if transport == "native" else bridge_cache.get(port, False),
            "models": list((record.get("models") or {}).keys()),
            "default_model": record.get("default_model"),
            "console_url": record.get("console_url")
            or (registry.preset(record.get("preset_id") or provider_id) or {}).get("console_url"),
            "requires_key": record.get("requires_key", True),
            "has_key": bool(stored_key),
            "key_hint": secrets.mask(stored_key) if stored_key else "未配置",
            "is_current": provider_id == current,
            "notes": record.get("notes"),
            "models_synced_at": record.get("models_synced_at"),
            "quota_tokens": record.get("quota_tokens"),
            "preset_id": (registry.preset(record.get("preset_id") or provider_id) or {}).get("id"),
        }
        if include_balance:
            item["balance"] = balance_module.query(record, secrets.load(provider_id))
        overview.append(item)
    return overview


def set_context_window(provider_id: str, model_id: str, window: Optional[int]) -> Dict:
    """给某个模型手动指定上下文窗口。

    为什么要能手改：目录里的窗口是按模型名猜的（见 registry.MODEL_HINTS），
    服务商改了规格、或者同一个模型在不同套餐下窗口不同，猜的值就会错。
    窗口一旦写小了，切换后就会陷入反复压缩；写大了会被服务商拒绝。
    所以这个值必须让用户能改，并且改完立刻重生成目录。
    """
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    models = record.get("models") or {}
    if model_id not in models:
        raise SwitchError("该平台没有名为 %s 的模型" % model_id)

    overrides = record.setdefault("model_overrides", {})
    entry = dict(overrides.get(model_id) or {})
    if window is None:
        entry.pop("context_window", None)
    else:
        window = int(window)
        # 下限给足一个真实可用的会话，上限别到荒谬的量级
        if window < 4096:
            raise SwitchError("上下文窗口太小了：至少 4096 tokens")
        if window > 10_000_000:
            raise SwitchError("上下文窗口太大了：上限 10,000,000 tokens")
        entry["context_window"] = window
    if entry:
        overrides[model_id] = entry
    else:
        overrides.pop(model_id, None)
    record["model_overrides"] = overrides
    state_module.save(state)

    # 目录必须跟着重生成，否则 Codex 读到的还是旧窗口
    catalog_module.write_catalog(record["id"], record, list(models.keys()))
    # 正在用这个模型的话，压缩触发点也得跟着改，否则新窗口在会话里不生效
    applied = _rewrite_if_current(record, model_id)
    return {
        "provider": provider_id,
        "model": model_id,
        "config_updated": applied,
        "context_window": window,
        "effective": contextguard_module.effective_window(
            {"context_window": window or 0,
             "effective_context_window_percent": catalog_module.DEFAULT_EFFECTIVE_PERCENT}),
        "auto_compact_limit": (contextguard_module.auto_compact_limit(
            contextguard_module.effective_window(
                {"context_window": window or 0,
                 "effective_context_window_percent": catalog_module.DEFAULT_EFFECTIVE_PERCENT}))
            if window else None),
    }


def set_reasoning_effort(provider_id: str, model_id: str, effort: Optional[str]) -> Dict:
    """设置某个模型的思考强度（none / low / medium / high / xhigh）。

    为什么需要它：思考强度写在目录的 default_reasoning_level 里，也在
    config.toml 的 model_reasoning_effort 里。两处不一致时，Codex 用 config
    的值发请求，但下拉框里显示的却是目录里的档位，用户会以为改了没生效。
    所以这里一次改两处：目录重生成，config 也跟着重写。
    """
    level = capabilities_module.normalize_effort(effort)
    if effort and not level:
        raise SwitchError("不认识的思考档位：%s（可选 %s）"
                          % (effort, " / ".join(capabilities_module.EFFORT_ORDER)))
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    models = record.get("models") or {}
    if model_id not in models:
        raise SwitchError("该平台没有名为 %s 的模型" % model_id)

    overrides = record.setdefault("model_overrides", {})
    entry = dict(overrides.get(model_id) or {})
    if level is None:
        entry.pop("default_reasoning_level", None)
    else:
        entry["default_reasoning_level"] = level
    if entry:
        overrides[model_id] = entry
    else:
        overrides.pop(model_id, None)
    record["model_overrides"] = overrides
    state_module.save(state)
    catalog_module.write_catalog(record["id"], record, list(models.keys()))
    applied = _rewrite_if_current(record, model_id)
    return {"provider": provider_id, "model": model_id, "effort": level,
            "label": capabilities_module.EFFORT_TEXT.get(level or "", level or "跟随平台默认"),
            "config_updated": applied}


def set_model_capability(provider_id: str, model_id: str, vision: Optional[bool],
                         tools: Optional[bool] = None) -> Dict:
    """手动修正一个模型的能力声明。

    典型场景：实测发现这个模型读不了图，但目录里还写着支持图片，
    Codex 就会一直把图塞给它，然后得到空回答。改完立刻重生成目录。
    """
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    models = record.get("models") or {}
    if model_id not in models:
        raise SwitchError("该平台没有名为 %s 的模型" % model_id)

    overrides = record.setdefault("model_overrides", {})
    entry = dict(overrides.get(model_id) or {})
    saved = dict(entry.get("capabilities") or {})
    if vision is None:
        entry.pop("input_modalities", None)
        saved.pop("vision", None)
    else:
        entry["input_modalities"] = ["text", "image"] if vision else ["text"]
        saved["vision"] = "yes" if vision else "no"
        saved["verified_at"] = capabilities_module._today()
        saved["note"] = "手动指定"
    if tools is not None:
        saved["tools"] = "yes" if tools else "no"
        entry["supports_parallel_tool_calls"] = bool(tools)
    if saved:
        entry["capabilities"] = saved
    if entry:
        overrides[model_id] = entry
    else:
        overrides.pop(model_id, None)
    record["model_overrides"] = overrides
    state_module.save(state)
    catalog_module.write_catalog(record["id"], record, list(models.keys()))
    return {"provider": provider_id, "model": model_id,
            "vision": saved.get("vision"), "tools": saved.get("tools")}


def capability_matrix(provider_id: Optional[str] = None) -> List[Dict]:
    """能力矩阵：哪些能力是实测的、哪些只是按名字猜的，一眼能分清。"""
    state = state_module.load()
    targets = ([provider_id] if provider_id
               else state_module.provider_ids(state))
    rows: List[Dict] = []
    for target in targets:
        record = state_module.get_provider(state, target)
        if not record:
            continue
        for row in capabilities_module.matrix(target, record):
            row["provider"] = target
            row["provider_label"] = record.get("label") or target
            rows.append(row)
    return rows


def probe_capabilities(provider_id: str, model_id: Optional[str] = None,
                       apply_result: bool = False, timeout: int = 30) -> Dict:
    """拿真实请求测一个（或所有）模型的能力。

    apply_result=True 时把测出来的结论写回目录，
    从此 Codex 按真实能力发请求，不再把图发给读不了图的模型。
    """
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    api_key = secrets.load(provider_id) if record.get("requires_key", True) else None
    if record.get("requires_key", True) and not api_key:
        raise SwitchError("该平台的密钥不在系统钥匙串里，无法实测")

    models = list((record.get("models") or {}).keys())
    if not models:
        raise SwitchError("该平台还没有模型，请先刷新模型列表")
    targets = [model_id] if model_id else models
    unknown = [item for item in targets if item not in models]
    if unknown:
        raise SwitchError("该平台没有名为 %s 的模型" % unknown[0])

    results = []
    for target in targets:
        try:
            outcome = capabilities_module.probe(provider_id, record, target, api_key, timeout=timeout)
        except capabilities_module.ProbeError as exc:
            results.append({"model": target, "error": str(exc)})
            continue
        if apply_result:
            outcome["applied"] = capabilities_module.apply_result(provider_id, record, target, outcome)
        results.append(outcome)

    if apply_result:
        state_module.save(state)
        catalog_module.write_catalog(record["id"], record, models)
    return {"provider": provider_id, "transport": record.get("transport"),
            "results": results, "applied": apply_result}


def _rewrite_if_current(record: Dict, model_id: str) -> bool:
    """如果 Codex 当前正用这个平台+模型，把新设置写进 config.toml。

    改了目录却不动 config，用户看到的就是「界面上说改好了，实际还在用旧值」。
    """
    config = paths.config_path()
    if not config.exists():
        return False
    try:
        current = configfile.read_top_level(
            config.read_text(), ("model_provider", "model"))
    except Exception:  # noqa: BLE001 - 读不出来就跳过，不影响记录本身
        return False
    if current.get("model_provider") != record.get("id"):
        return False
    if current.get("model") not in (None, "", model_id):
        return False
    try:
        text = config.read_text()
        new_text = configfile.rewrite_model_settings(
            text, provider_settings(record, model_id, record.get("id")))
        with platform_compat.file_lock(paths.lock_file()):
            configfile.backup(config)
            configfile.atomic_write(config, new_text, text)
        return True
    except Exception:  # noqa: BLE001 - 写配置失败不该让设置本身失败
        return False


def set_quota(provider_id: str, tokens: Optional[int]) -> Dict:
    """记录这个平台套餐的 token 总量，用来算本机用量的百分比。

    分母完全由用户自己填（例如套餐页面写的“每月 5 亿 tokens”），
    我们不猜、也不从别处推断。
    """
    state = state_module.load()
    record = state_module.get_provider(state, provider_id)
    if not record:
        raise SwitchError("没有找到平台：%s" % provider_id)
    if tokens is None:
        record.pop("quota_tokens", None)
    else:
        if tokens <= 0:
            raise SwitchError("额度必须是正整数")
        record["quota_tokens"] = int(tokens)
    state_module.save(state)
    return record


def usage_with_quota(provider_id: str, quota_tokens: Optional[int], used_tokens: int) -> Dict:
    """算出本机用量的占比。没有填额度就不给百分比，避免误导。"""
    result = {"used_tokens": used_tokens, "quota_tokens": quota_tokens}
    if quota_tokens:
        percent = min(100.0, round(used_tokens / quota_tokens * 100, 2))
        result["percent"] = percent
        result["remaining_tokens"] = max(0, quota_tokens - used_tokens)
    return result
