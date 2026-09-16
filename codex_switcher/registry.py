"""平台预设库。

这里的 URL 都是各家官方文档公开的入口。预设只描述“怎么连”，不保存任何账号信息。

字段含义：
  base_url    —— 平台真实地址
  models_url  —— 拉取模型清单的地址（有些平台和 base_url 不同）
  transport   —— native：平台自带 Responses 接口，可以直接用
                 auto  ：添加时自动探测，探不到就走本地协议桥翻译
  balance     —— 余额/额度适配器；None 表示该平台没有公开额度接口
  console_url —— 查额度、充值、看账单的官网页面

关于协议：新版 Codex 只接受 `wire_api = "responses"`。写成 "chat" 会直接
报 `wire_api = "chat" is no longer supported` 并拒绝启动。所以本工具永远生成
responses；只支持 Chat Completions 的平台由本地协议桥（codex_switcher.bridge）转换。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------- 模型能力推断

MODEL_HINTS = [
    (r"minimax-m3", {"context": 1000000, "modalities": ["text", "image"], "efforts": ["none", "high"]}),
    (r"minimax-m2", {"context": 204800, "modalities": ["text"], "efforts": ["none", "high"]}),
    (r"deepseek-v4-pro|deepseek-reasoner", {"context": 262144, "modalities": ["text"], "efforts": ["high"]}),
    (r"deepseek", {"context": 131072, "modalities": ["text"], "efforts": ["high"]}),
    (r"glm-5|glm-4\.7|glm-4\.6", {"context": 204800, "modalities": ["text"], "efforts": ["high"]}),
    (r"glm", {"context": 204800, "modalities": ["text"], "efforts": ["high"]}),
    (r"kimi-k2|moonshot-v1-128k", {"context": 262144, "modalities": ["text"], "efforts": ["none"]}),
    (r"moonshot", {"context": 131072, "modalities": ["text"], "efforts": ["none"]}),
    (r"qwen.*(vl|omni)", {"context": 131072, "modalities": ["text", "image"], "efforts": ["none"]}),
    (r"qwen", {"context": 131072, "modalities": ["text"], "efforts": ["none"]}),
    (r"claude", {"context": 200000, "modalities": ["text", "image"], "efforts": ["high"]}),
    (r"gemini", {"context": 1000000, "modalities": ["text", "image"], "efforts": ["none"]}),
    (r"gpt-5|gpt-6|o[34]", {"context": 400000, "modalities": ["text", "image"], "efforts": ["low", "medium", "high"]}),
    (r"gpt-4o|gpt-4\.1", {"context": 128000, "modalities": ["text", "image"], "efforts": ["none"]}),
    (r"grok", {"context": 256000, "modalities": ["text"], "efforts": ["none"]}),
    (r"llama|mixtral|mistral", {"context": 131072, "modalities": ["text"], "efforts": ["none"]}),
    (r"glm-4v|vision|vl", {"context": 131072, "modalities": ["text", "image"], "efforts": ["none"]}),
]

DEFAULT_HINT = {"context": 131072, "modalities": ["text"], "efforts": ["none"]}


def hint_for(model_id: str) -> Dict:
    """按模型名推断上下文窗口、输入模态与可用推理档位。"""
    lowered = (model_id or "").lower()
    for pattern, hint in MODEL_HINTS:
        if re.search(pattern, lowered):
            return dict(hint)
    return dict(DEFAULT_HINT)


# ------------------------------------------------------------------ 余额适配器

BALANCE_DEEPSEEK = {"kind": "builtin", "adapter": "deepseek"}
BALANCE_MOONSHOT = {"kind": "builtin", "adapter": "moonshot"}
BALANCE_SILICONFLOW = {"kind": "builtin", "adapter": "siliconflow"}
BALANCE_OPENROUTER = {"kind": "builtin", "adapter": "openrouter"}


# ---------------------------------------------------------------------- 预设表

PRESETS: List[Dict] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "config_name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models_url": "https://api.deepseek.com/models",
        "transport": "native",
        "balance": BALANCE_DEEPSEEK,
        "console_url": "https://platform.deepseek.com/usage",
        "known_models": ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "notes": "原生支持 Responses 协议，已实测可用；官方余额接口能显示真实剩余金额。",
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "config_name": "MiniMax",
        "base_url": "https://api.minimax.cn/v1",
        "models_url": "https://api.minimax.cn/v1/models",
        "transport": "native",
        "balance": None,
        "console_url": "https://platform.minimaxi.com/user-center/payment/balance",
        "known_models": [
            "MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5", "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1", "MiniMax-M2.1-highspeed", "MiniMax-M2",
        ],
        "notes": "原生支持 Responses 协议，已实测可用；官方未开放额度查询接口，套餐余量请在官网查看。",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "config_name": "GLM Coding Plan",
        "base_url": "https://open.bigmodel.cn/api/v1",
        "models_url": "https://open.bigmodel.cn/api/paas/v4/models",
        "transport": "native",
        "balance": None,
        "console_url": "https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "known_models": [
            "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1",
            "glm-5", "glm-5-turbo", "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air",
        ],
        "notes": "原生支持 Responses 协议；官方未开放额度查询接口，套餐余量请在官网查看。",
    },
    {
        "id": "moonshot",
        "label": "月之暗面 Kimi",
        "config_name": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "models_url": "https://api.moonshot.cn/v1/models",
        "transport": "auto",
        "balance": BALANCE_MOONSHOT,
        "console_url": "https://platform.moonshot.cn/console/info",
        "known_models": ["kimi-k2-turbo-preview", "kimi-k2-0905-preview", "moonshot-v1-128k"],
        "notes": "官方余额接口可用。",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼（通义千问）",
        "config_name": "DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://bailian.console.aliyun.com/",
        "known_models": ["qwen3-max", "qwen3-coder-plus", "qwen-plus", "qwen-turbo", "qwen3-vl-plus"],
        "notes": "额度属于阿里云账户，接口不返回，请在控制台查看。",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "config_name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models_url": "https://api.siliconflow.cn/v1/models",
        "transport": "auto",
        "balance": BALANCE_SILICONFLOW,
        "console_url": "https://cloud.siliconflow.cn/account/balance",
        "known_models": ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen3-235B-A22B", "moonshotai/Kimi-K2-Instruct"],
        "notes": "官方账户接口可用，能显示余额。",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "config_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "transport": "auto",
        "balance": BALANCE_OPENROUTER,
        "console_url": "https://openrouter.ai/settings/credits",
        "known_models": [],
        "notes": "模型清单公开可读；密钥支持查询额度上限与已用金额。",
    },
    {
        "id": "groq",
        "label": "Groq",
        "config_name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://console.groq.com/settings/billing",
        "known_models": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        "notes": "按速率限制计费，无余额接口。",
    },
    {
        "id": "together",
        "label": "Together AI",
        "config_name": "Together",
        "base_url": "https://api.together.xyz/v1",
        "models_url": "https://api.together.xyz/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://api.together.ai/settings/billing",
        "known_models": [],
        "notes": None,
    },
    {
        "id": "mistral",
        "label": "Mistral",
        "config_name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "models_url": "https://api.mistral.ai/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://console.mistral.ai/billing/",
        "known_models": [],
        "notes": None,
    },
    {
        "id": "xai",
        "label": "xAI Grok",
        "config_name": "xAI",
        "base_url": "https://api.x.ai/v1",
        "models_url": "https://api.x.ai/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://console.x.ai/",
        "known_models": ["grok-4", "grok-4-fast", "grok-code-fast-1"],
        "notes": None,
    },
    {
        "id": "cerebras",
        "label": "Cerebras",
        "config_name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "models_url": "https://api.cerebras.ai/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://cloud.cerebras.ai/",
        "known_models": [],
        "notes": None,
    },
    {
        "id": "fireworks",
        "label": "Fireworks AI",
        "config_name": "Fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "models_url": "https://api.fireworks.ai/inference/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://fireworks.ai/account/billing",
        "known_models": [],
        "notes": None,
    },
    {
        "id": "deepinfra",
        "label": "DeepInfra",
        "config_name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "models_url": "https://api.deepinfra.com/v1/openai/models",
        "transport": "auto",
        "balance": None,
        "console_url": "https://deepinfra.com/dash/billing",
        "known_models": [],
        "notes": None,
    },
    {
        "id": "ollama-local",
        "label": "Ollama（本机）",
        "config_name": "Ollama Local",
        "base_url": "http://127.0.0.1:11434/v1",
        "models_url": "http://127.0.0.1:11434/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": None,
        "known_models": [],
        "requires_key": False,
        "notes": "本机模型，不需要密钥也没有费用。id 特意叫 ollama-local —— "
                 "ollama 是 Codex 的保留名，用它会看不到模型列表。",
    },
    {
        "id": "lmstudio-local",
        "label": "LM Studio（本机）",
        "config_name": "LM Studio Local",
        "base_url": "http://127.0.0.1:1234/v1",
        "models_url": "http://127.0.0.1:1234/v1/models",
        "transport": "auto",
        "balance": None,
        "console_url": None,
        "known_models": [],
        "requires_key": False,
        "notes": "本机模型，不需要密钥。id 特意叫 lmstudio-local，避免撞上 Codex 保留名。",
    },
    {
        "id": "custom",
        "label": "自定义平台 / 中转站",
        "config_name": "Custom",
        "base_url": "",
        "models_url": "",
        "transport": "auto",
        "balance": None,
        "console_url": None,
        "known_models": [],
        "notes": "自己填 Base URL 与模型清单，适用于中转站或私有部署。",
    },
]

PRESET_BY_ID = {item["id"]: item for item in PRESETS}

# Codex 自己占用的 provider id。用这些名字会让 Codex 忽略我们的模型目录。
RESERVED_PROVIDER_IDS = frozenset({"openai", "ollama", "lmstudio", "codex", "azure"})

# Codex 只接受这一种协议写法
WIRE_API = "responses"


def preset(provider_id: str) -> Optional[Dict]:
    return PRESET_BY_ID.get(provider_id)


def preset_list() -> List[Dict]:
    """给界面用的精简预设列表。"""
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "base_url": item["base_url"],
            "transport": item.get("transport", "auto"),
            "has_balance_api": item["balance"] is not None,
            "console_url": item.get("console_url"),
            "notes": item.get("notes"),
            "requires_key": item.get("requires_key", True),
            "known_models": list(item.get("known_models") or []),
        }
        for item in PRESETS
    ]


def derive_models_url(base_url: str) -> str:
    """用户手填 Base URL 时，猜一个模型清单地址。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1") or base.endswith("/openai"):
        return base + "/models"
    if "/v1" in base:
        return base + "/models"
    return base + "/v1/models"
