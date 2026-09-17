"""生成 Codex 的 model_catalog_json。

Codex 读取这个文件来填充输入框旁的模型下拉列表。为每个模型生成一份完整条目，
这样第三方平台的模型才会像官方模型一样出现在列表里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

from . import paths
from .registry import hint_for

# Codex 拿 context_window 乘这个百分比，得到它真正认可的可用窗口。
# 实测与它上报的 model_context_window 完全一致：131072 × 0.95 = 124518。
DEFAULT_EFFECTIVE_PERCENT = 95

EFFORT_LABELS = {
    "none": "Think-Off",
    "low": "Fast reasoning",
    "medium": "Standard reasoning",
    "high": "Deep reasoning",
    "xhigh": "Extra-high reasoning",
    "ultra": "Ultra reasoning",
    "max": "Maximum reasoning",
}


def build_model_entry(
    model_id: str,
    provider_label: str,
    overrides: Dict = None,
) -> Dict:
    """单个模型条目。overrides 允许用户手动指定上下文窗口等信息。"""
    overrides = overrides or {}
    hint = hint_for(model_id)
    context = int(overrides.get("context_window") or hint["context"])
    modalities = list(overrides.get("input_modalities") or hint["modalities"])
    efforts = list(overrides.get("efforts") or hint["efforts"])
    if not efforts:
        efforts = ["none"]

    default_effort = overrides.get("default_reasoning_level") or (
        "high" if "high" in efforts else efforts[0]
    )
    description = overrides.get("description") or (
        ("%s · 支持图片" % model_id) if "image" in modalities else ("%s · 纯文本" % model_id)
    )

    entry = {
        "slug": model_id,
        "display_name": overrides.get("display_name") or model_id,
        "description": description,
        "default_reasoning_level": default_effort,
        "supported_reasoning_levels": [
            {"effort": effort, "description": EFFORT_LABELS.get(effort, effort)} for effort in efforts
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": int(overrides.get("priority", 0)),
        "base_instructions": "You are Codex, a coding agent based on %s. "
                             "You and the user share a workspace and collaborate to complete the user's goals."
                             % model_id,
        "supports_reasoning_summaries": bool(overrides.get("supports_reasoning_summaries", True)),
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "apply_patch_tool_type": "freeform",
        "prefer_websockets": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": bool(overrides.get("supports_parallel_tool_calls", True)),
        "experimental_supported_tools": [],
        "input_modalities": modalities,
        "context_window": context,
        "max_context_window": context,
        "effective_context_window_percent": DEFAULT_EFFECTIVE_PERCENT,
    }
    return entry


def build_catalog(provider: Dict, model_ids: Iterable[str]) -> Dict:
    """按平台生成的完整目录。"""
    overrides_map: Dict[str, Dict] = provider.get("model_overrides") or {}
    ordered: List[str] = list(dict.fromkeys(model_ids))
    configured = provider.get("models") or {}
    ordered = [item for item in configured if item in ordered] + [
        item for item in ordered if item not in configured
    ]
    entries = []
    for index, model_id in enumerate(ordered):
        overrides = dict(overrides_map.get(model_id) or {})
        overrides.setdefault("priority", index)
        entries.append(build_model_entry(model_id, provider.get("label", ""), overrides))
    return {"models": entries}


def catalog_path(provider_id: str) -> Path:
    return paths.catalog_dir() / (provider_id + ".json")


def write_catalog(provider_id: str, provider: Dict, model_ids: Iterable[str]) -> Path:
    document = build_catalog(provider, model_ids)
    target = catalog_path(provider_id)
    paths.ensure_dir(target.parent)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return target


def read_catalog_models(path) -> List[str]:
    try:
        document = json.loads(Path(path).read_text())
    except Exception:
        return []
    return [entry.get("slug") for entry in document.get("models", []) if entry.get("slug")]
