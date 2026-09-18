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

# 工具输出截断上限（字节）。Codex 自己的兜底预设也是这个值
# （见 openai_models.rs: TruncationPolicyConfig::bytes(10_000)），
# 所以这不是我们设的限制；但它是可覆盖的 —— 工具输出被截得太狠时调大即可。
TRUNCATION_LIMIT_BYTES = 10000

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
    *,
    is_official: bool = False,
) -> Dict:
    """单个模型条目。overrides 允许用户手动指定上下文窗口等信息。

    is_official:
        True  —— 官方 OpenAI：Codex App 的全部内部工具（tool_search /
                 computer_use / automation_update / App 内工具）都可下发，
                 客户端会按模型能力正常处理。
        False —— 第三方平台：默认关闭「include_*_usage_instructions」三道开关。
                 Codex 看到 False 就不会往请求里塞 `tool_search` 这类 OpenAI-only
                 的工具，第三方服务端也就不会再因 `tools.N: tool type
                 "tool_search" is not supported` 把整轮拒收。
        用户可以在 `model_overrides.<model>` 里强行把这三道开关打开，但默认
        是关闭 —— 默认开是历史 bug，2026-09 在 Kimi 模型上复现过。
    """
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
    # 是否给这个模型开「看原图」和「搜索工具」。默认给足，
    # 只有用户明确关掉才收窄 —— 默认关掉就是一种阉割。
    supports_image = "image" in modalities

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
        "truncation_policy": {"mode": "bytes", "limit": TRUNCATION_LIMIT_BYTES},
        "supports_parallel_tool_calls": bool(overrides.get("supports_parallel_tool_calls", True)),
        "experimental_supported_tools": [],
        "input_modalities": modalities,
        "context_window": context,
        "max_context_window": context,
        "effective_context_window_percent": DEFAULT_EFFECTIVE_PERCENT,

        # ---- 下面这几个是 Codex 的「能力总开关」。
        #
        # 它们在 Codex 里的定义都带 #[serde(default)]，也就是：字段不写 = false。
        # 以前我们根本没生成这几个字段，Codex 于是把第三方模型当成
        # 「不支持 MCP / 不支持插件 / 不支持搜索 / 看不了原图」来处理 ——
        # 这就是用户说的「能力被严重阉割」的真正来源，跟平台无关，是我们写的。
        #
        # 参照 codex-rs/protocol/src/openai_models.rs 的 ModelInfo：
        #   include_skills_usage_instructions  —— 是否把 skills/MCP 用法写进提示词
        #   include_plugin_usage_instructions  —— 是否把插件用法写进提示词
        #   include_apps_usage_instructions    —— 是否把 apps 用法写进提示词
        #   supports_search_tool               —— 是否提供搜索工具
        #   supports_image_detail_original     —— 图片能否用 detail=original
        #   web_search_tool_type               —— 搜索工具形态：text / text_and_image
        #   supports_reasoning_summary_parameter —— 是否接受 reasoning.summary
        #
        # 第三方平台与官方在这四道开关上策略不同（见 build_model_entry 注释）：
        #   include_skills_usage_instructions —— 第三方依然可用 skills（本地加载）
        #   include_plugin_usage_instructions —— 第三方依然可用 plugins（本地）
        #   include_apps_usage_instructions   —— App 内工具（tool_search 等）只
        #                                          官方能用，第三方一律关闭。
        #   supports_search_tool              —— 决定了 Codex 是否往 outbound
        #                                          tools 数组里塞 tool_search（spec_plan.rs
        #                                          第 624 行的注册条件）。第三方平台
        #                                          服务端不认这个工具名，必须 False，
        #                                          否则 12:01 那种
        #                                          `tools.13: tool type "tool_search" is not supported`
        #                                          的 400 就会复现。
        # 用户在 model_overrides.<model> 里写什么就以什么为准，不强制。
        "include_skills_usage_instructions": bool(
            overrides.get("include_skills_usage_instructions", True)),
        "include_plugin_usage_instructions": bool(
            overrides.get("include_plugin_usage_instructions", True)),
        "include_apps_usage_instructions": bool(
            overrides.get("include_apps_usage_instructions", is_official)),
        "supports_search_tool": bool(
            overrides.get("supports_search_tool", is_official)),
        "supports_image_detail_original": bool(
            overrides.get("supports_image_detail_original", supports_image)),
        "web_search_tool_type": "text_and_image" if supports_image else "text",
        "supports_reasoning_summary_parameter": True,
    }
    return entry


def build_catalog(provider: Dict, model_ids: Iterable[str],
                  *, is_official: bool = False) -> Dict:
    """按平台生成的完整目录。is_official=True 时开启 include_*_usage_instructions。"""
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
        entries.append(build_model_entry(model_id, provider.get("label", ""),
                                        overrides, is_official=is_official))
    return {"models": entries}


def catalog_path(provider_id: str) -> Path:
    return paths.catalog_dir() / (provider_id + ".json")


def write_catalog(provider_id: str, provider: Dict, model_ids: Iterable[str],
                  *, is_official: bool = False) -> Path:
    document = build_catalog(provider, model_ids, is_official=is_official)
    target = catalog_path(provider_id)
    paths.ensure_dir(target.parent)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return target


def model_entry(path, model_id: str) -> Optional[Dict]:
    """从已生成的目录里取回单个模型的条目。"""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    for entry in document.get("models", []):
        if entry.get("slug") == model_id:
            return entry
    return None


def read_catalog_models(path) -> List[str]:
    try:
        document = json.loads(Path(path).read_text())
    except Exception:
        return []
    return [entry.get("slug") for entry in document.get("models", []) if entry.get("slug")]
