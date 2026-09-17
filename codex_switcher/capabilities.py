"""模型能力：看得见、测得出来、测完能落回目录。

用户遇到的实际情况是「切到第三方模型以后，一些能力悄悄不见了」——
发图它当没看见、让它想它不想、工具调不动。原因有三层：

  1. 目录里写的能力是按模型名猜的。猜错了，Codex 就会把图片发给一个
     根本读不了图的模型，或者反过来，压根不给你开思考档位。
  2. 各家对「思考强度」的说法不一样：OpenAI 系叫 reasoning_effort，
     智谱要 thinking，MiniMax 认 reasoning_split。协议桥如果不翻译，
     你在界面上选了「深度思考」，到了平台那边等于什么都没说。
  3. OpenAI 的服务器侧工具（联网搜索、画图、computer use）只有 OpenAI 自己
     有。第三方平台没有就是没有，这个必须如实标出来，不能假装存在。

这个模块做三件事：
  · matrix()：把「猜的」和「实测的」分开列出来，让人知道哪个数能信
  · probe()：拿真实请求去测图 / 测思考 / 测工具调用，得出实测结论
  · apply()：把实测结论写进目录，Codex 从此按真实能力发请求

测出来的结论只有三种：yes / no / unknown。
拿不到证据就写 unknown —— 宁可说不知道，也不猜一个好看的答案。
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import registry

# ------------------------------------------------------------------ 思考强度

# Codex 认识的档位，从低到高。目录里的 supported_reasoning_levels 从这里取。
EFFORT_ORDER = ["none", "low", "medium", "high", "xhigh"]

EFFORT_TEXT = {
    "none": "不思考（最快）",
    "low": "轻度思考",
    "medium": "标准思考",
    "high": "深度思考",
    "xhigh": "超深度思考",
}

# 各家对「思考」的入参写法。协议桥按平台挑一个。
#   reasoning_effort —— OpenAI 兼容写法（多数平台认）
#   thinking         —— 智谱 GLM
#   reasoning_split   —— MiniMax
THINKING_STYLE = {
    "zhipu": "thinking",
    "minimax": "reasoning_split",
}

# 只有 OpenAI 自己提供的服务器侧工具。第三方平台一律没有。
OPENAI_ONLY_TOOLS = {
    "web_search_call": "联网搜索",
    "image_generation_call": "生成图片",
    "computer_call": "操作电脑",
    "file_search_call": "文件检索",
    "code_interpreter_call": "代码解释器",
}


def normalize_effort(value: Optional[str]) -> Optional[str]:
    """把用户写的档位收敛到 Codex 认识的集合里。写错就返回 None。"""
    text = (value or "").strip().lower()
    if not text:
        return None
    alias = {
        "off": "none", "0": "none", "false": "none", "minimal": "low",
        "mid": "medium", "default": "medium", "deep": "high", "max": "xhigh",
        "ultra": "xhigh",
    }
    text = alias.get(text, text)
    return text if text in EFFORT_ORDER else None


def thinking_payload(provider_id: str, effort: Optional[str]) -> Dict:
    """协议桥要把「思考强度」翻译成平台听得懂的字段。

    认不出来就退回最通用的 reasoning_effort：平台不认识顶多忽略，
    总比什么都不发要好。
    """
    if not effort or effort == "none":
        return {}
    style = THINKING_STYLE.get((provider_id or "").strip().lower(), "reasoning_effort")
    if style == "thinking":
        return {"thinking": {"type": "enabled"}}
    if style == "reasoning_split":
        return {"reasoning_split": True}
    return {"reasoning_effort": effort}


# -------------------------------------------------------------- 实测结论表

# 官方公开说明里写明的能力。带出处，方便自己核对。
# 这一层存在的意义：实测探针会受接口写法、图片大小、平台临时故障影响，
# 实测说「不支持」而官方说「支持」时，多半是我们没测对 —— 两个都显示出来，
# 而不是拿一次失败的探测去否定官方文档。
#
# 值一律抄自各平台官方文档（2026-09-17 核对），不写没出处的东西。
DOCUMENTED_FACTS: Dict[str, Dict] = {
    "minimax/MiniMax-M3": {
        "vision": "yes", "reasoning": "yes", "tools": "yes",
        "context": 1048576,
        "source": "MiniMax 官方文档：M3 原生支持文本 / 图片 / 视频输入，1M 长上下文，"
                  "thinking 可用 adaptive 或 disabled 控制；messages 支持 tool call 内容，"
                  "tools 支持 function tools。",
        "url": "https://platform.minimax.io/docs/api-reference/text-chat-openai",
    },
    "deepseek/deepseek-flash": {
        "vision": "yes", "reasoning": "yes", "tools": "yes",
        "context": 1048576,
        "source": "DeepSeek 官方文档（模型 & 价格）：deepseek-flash（V4.1-Flash）"
                  "支持思考/非思考模式、1M 上下文、Tool Calls、Responses API、图像理解。",
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    },
    "deepseek/deepseek-v4-pro": {
        "vision": "no", "reasoning": "yes", "tools": "yes",
        "context": 1048576,
        "source": "DeepSeek 官方文档（模型 & 价格）：deepseek-v4-pro 支持思考模式、"
                  "1M 上下文、Tool Calls、Responses API；图像理解一栏为「不支持」。",
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    },
    "glm/glm-5.3": {
        "vision": "no", "reasoning": "yes", "tools": "yes",
        "context": 1048576,
        "source": "智谱官方文档（GLM-5.3）：仅支持处理文本模态，1M 上下文，最大输出 128K；"
                  "始终启用思考（low / high / max，不支持禁用）；支持 Function Calling；"
                  "提供 OpenAI Response 协议端点 https://open.bigmodel.cn/api/v1，"
                  "可直接接进 Codex。",
        "url": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
    },
}

# 按名字匹配的官方说明。一整条产品线共用同一句说明时用这个，免得重复写八遍。
# 顺序有意义：先匹配具体的，再匹配整条产品线。
DOCUMENTED_RULES: List[Tuple[str, Dict]] = [
    (r"^minimax-m2", {
        "vision": "no", "reasoning": "yes", "tools": "yes",
        "source": "MiniMax 官方文档：M2.x 系列（M2.7 / M2.5 / M2.1 / M2）"
                  "只支持文本与 function tools，不接受图片或视频输入；"
                  "thinking 只能开启、不能禁用。",
        "url": "https://platform.minimax.io/docs/api-reference/text-chat-openai",
    }),
    (r"^minimax", {
        "reasoning": "yes", "tools": "yes",
        "source": "MiniMax 官方文档：这批对话模型都支持 thinking 与 function tools。",
        "url": "https://platform.minimax.io/docs/api-reference/text-chat-openai",
    }),
    (r"^deepseek-(flash|v4-flash)", {
        "vision": "yes", "reasoning": "yes", "tools": "yes",
        "source": "DeepSeek 官方文档：Flash 线支持图像理解、思考模式、Tool Calls 与 Responses API。",
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    }),
    (r"^deepseek", {
        "reasoning": "yes", "tools": "yes",
        "source": "DeepSeek 官方文档：对话模型均支持思考模式、Tool Calls 与 Responses API。",
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
    }),
    # 只匹配 glm-5.3 本体：Flash 是另一条产品线（原生多模态），
    # 官方文档没写它的输入模态，不能拿 5.3 的结论套上去。
    (r"^glm-5\.3$", {
        "vision": "no", "reasoning": "yes", "tools": "yes",
        "source": "智谱官方文档：GLM-5.3 仅支持文本模态，始终启用思考，支持 Function Calling。",
        "url": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
    }),
]

# 各平台的接入方式与官方文档入口：界面上直接告诉用户「怎么配进 Codex」。
PLATFORM_DOCS: Dict[str, Dict] = {
    "minimax": {
        "docs": "https://platform.minimax.io/docs/api-reference/text-chat-openai",
        "hint": "Base URL 填 https://api.minimax.cn/v1（OpenAI 兼容），本工具按原生直连接入。",
    },
    "deepseek": {
        "docs": "https://api-docs.deepseek.com/zh-cn/guides/responses_api",
        "hint": "换新用法支持 Responses API：Base URL 填 https://api.deepseek.com，"
                "Codex 直接说 Responses 协议，不用本地桥。",
    },
    "glm": {
        "docs": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
        "hint": "官方提供 OpenAI Response 协议端点 https://open.bigmodel.cn/api/v1，"
                "填这个即可原生接入。",
    },
    "moonshot": {
        "docs": "https://platform.moonshot.cn/docs/api/chat",
        "hint": "Kimi 只有 OpenAI 兼容的 /chat/completions，需要走本工具的本地协议桥翻译。",
    },
}

# 这些是拿真实请求测出来的（日期见 verified_at），不是按名字猜的。
# 没测过的模型不会出现在这里，fallback 到官方文档或按名字推断。
VERIFIED_FACTS: Dict[str, Dict] = {
    "minimax/MiniMax-M3": {
        "vision": "yes", "reasoning": "yes", "tools": "yes",
        "verified_at": "2026-09-17",
        "note": "实测：红底 2 个 / 3 个蓝方块的计数题都答对；平台返回思考 token；"
                "能发起函数调用。与官方「M3 多模态」一致。",
    },
    "minimax/MiniMax-M2.1": {
        "vision": "no", "reasoning": "unknown", "tools": "yes",
        "verified_at": "2026-09-17",
        "note": "实测：模型自己回答「I can't see the image」，能发起函数调用。",
    },
    "deepseek/deepseek-flash": {
        "vision": "yes", "reasoning": "yes", "tools": "yes",
        "verified_at": "2026-09-17",
        "note": "实测：两张数方块图都答对；思考档位生效并返回 reasoning_tokens；"
                "能发起函数调用。",
    },
    "deepseek/deepseek-v4-pro": {
        "vision": "unknown", "reasoning": "yes", "tools": "yes",
        "verified_at": "2026-09-17",
        "note": "实测：思考档位生效、能发起函数调用；"
                "读图没测出来 —— 平台拒绝了我们发的图片块（400 unknown variant）。",
    },
}

# 平台上的「生成」类模型（画图 / 视频 / 语音）是另一批独立模型、走独立接口。
# Codex 只会跟一个对话模型说话，调用不到这些接口 —— 如实说明，不假装支持。
GENERATION_NOTE = (
    "画图 / 生成视频 / 语音合成是各家平台上的独立模型与独立接口"
    "（如 image-01、video-01、speech-02），Codex 只连一个对话模型，用不到它们。"
)


def fact_key(provider_id: str, model_id: str) -> str:
    return "%s/%s" % ((provider_id or "").strip(), (model_id or "").strip())


def known_fact(provider_id: str, model_id: str) -> Optional[Dict]:
    return VERIFIED_FACTS.get(fact_key(provider_id, model_id))


def infer(model_id: str) -> Dict:
    """按模型名推断能力。出处是 registry.MODEL_HINTS，属于经验值不是实测值。"""
    hint = registry.hint_for(model_id)
    efforts = [item for item in (hint.get("efforts") or []) if item in EFFORT_ORDER]
    return {
        "vision": "yes" if "image" in (hint.get("modalities") or []) else "no",
        "reasoning": "yes" if any(item != "none" for item in efforts) else "no",
        "tools": "unknown",
        "efforts": efforts or ["none"],
        "context": int(hint.get("context") or 0),
        "source": "inferred",
        "note": "按模型名推断，未经实测",
    }


def documented_fact(provider_id: str, model_id: str) -> Optional[Dict]:
    exact = DOCUMENTED_FACTS.get(fact_key(provider_id, model_id))
    if exact:
        return exact
    lowered = (model_id or "").lower()
    for pattern, fact in DOCUMENTED_RULES:
        if re.search(pattern, lowered):
            return fact
    return None


def platform_docs(provider_id: str) -> Optional[Dict]:
    """这个平台的官方文档在哪、怎么接进 Codex。认不出来就返回 None。"""
    return PLATFORM_DOCS.get((provider_id or "").strip().lower())


def capability_row(provider_id: str, model_id: str, override: Optional[Dict] = None) -> Dict:
    """单个模型的能力行：实测优先，其次官方文档，最后才按名字推断。

    实测和官方说法打架时两个都留着（documented 字段 + conflict 标记），
    由人来判断，不悄悄选一个。
    """
    override = override or {}
    row = infer(model_id)
    documented = documented_fact(provider_id, model_id)
    if documented:
        row.update({key: documented[key] for key in ("vision", "reasoning", "tools", "context")
                    if key in documented})
        row["source"] = "documented"
        row["documented"] = {key: documented[key] for key in
                             ("vision", "reasoning", "tools") if key in documented}
        row["documented_url"] = documented.get("url")
        row["note"] = documented.get("source") or ""
    fact = known_fact(provider_id, model_id)
    if fact:
        measured = {key: fact[key] for key in ("vision", "reasoning", "tools") if key in fact}
        if documented:
            # 只有「测出了明确结论」才算和官方打架；unknown 是没测出来，不算冲突。
            row["conflict"] = [key for key, value in measured.items()
                               if value != "unknown" and documented.get(key)
                               and documented[key] != value]
        # 实测的 unknown 只说明「我们没测出来」，不能拿它去否定官方写明的结论
        # （deepseek-v4-pro 就踩过这个坑：官方说图像理解不支持，却被一次
        #   400 的探测结果盖成了「未测出」，界面上看着像没人知道）。
        usable = {key: value for key, value in measured.items() if value != "unknown"}
        row.update(usable)
        row["source"] = "verified" if usable else (row.get("source") or "inferred")
        row["verified_at"] = fact.get("verified_at")
        row["note"] = fact.get("note") or row.get("note") or ""
    # 用户手动改过 / 探测后写回的，以本地记录为准（人亲眼看到的更可信）
    saved = override.get("capabilities") if isinstance(override.get("capabilities"), dict) else None
    if saved:
        for key in ("vision", "reasoning", "tools"):
            if saved.get(key):
                row[key] = saved[key]
        manual = saved.get("manual") if isinstance(saved.get("manual"), dict) else {}
        # 手动标注的和自动实测的必须分得清：一个是「我亲眼见过」，
        # 一个是「机器跑出来的」，界面上标不同来源，人才能判断可信度。
        row["source"] = "manual" if any(manual.get(key) for key in CAPABILITY_KEYS) else "measured"
        row["verified_at"] = saved.get("verified_at")
        if saved.get("note"):
            row["note"] = saved["note"]
    if override.get("input_modalities"):
        row["vision"] = "yes" if "image" in override["input_modalities"] else "no"
        row["source"] = row.get("source") or "manual"
    if override.get("context_window"):
        row["context"] = int(override["context_window"])
    if override.get("default_reasoning_level"):
        row["effort"] = override["default_reasoning_level"]
    row["model"] = model_id
    return row


def matrix(provider_id: str, record: Optional[Dict] = None) -> List[Dict]:
    """一个平台下所有模型的能力矩阵。"""
    models = list((record or {}).get("models") or {})
    overrides = (record or {}).get("model_overrides") or {}
    return [capability_row(provider_id, model, overrides.get(model)) for model in models]


# ------------------------------------------------------------- 人工标注

# 三项能力，三态：yes / no / unknown（未测出）。
# 用户点标签循环切换，结论写进 model_overrides[model].capabilities，
# 和实测写回同一个存储位，但带 manual 标记以便区分来源。
CAPABILITY_KEYS = ("vision", "reasoning", "tools")
CAPABILITY_VALUES = ("yes", "no", "unknown")

# 手动标注 → 目录里对应的开关。标成「不支持」必须真的把开关关掉，
# 否则 Codex 还会照样把图发过去（这就是「阉割/误报」的另一面）。
def _apply_manual_side_effects(entry: Dict, key: str, value: str) -> None:
    if key == "vision":
        if value == "yes":
            entry["input_modalities"] = ["text", "image"]
        elif value == "no":
            entry["input_modalities"] = ["text"]
        else:
            entry.pop("input_modalities", None)
    elif key == "reasoning":
        if value == "yes":
            entry.setdefault("default_reasoning_level", "high")
        elif value == "no":
            entry["default_reasoning_level"] = "none"
        else:
            entry.pop("default_reasoning_level", None)
    elif key == "tools":
        if value in ("yes", "no"):
            entry["supports_parallel_tool_calls"] = (value == "yes")
        else:
            entry.pop("supports_parallel_tool_calls", None)


def set_manual_capability(provider_id: str, record: Dict, model_id: str,
                          key: str, value: str) -> Dict:
    """人工标注某一项能力。value 为 'yes'/'no'/'unknown'。

    'unknown' 表示清掉这条标注、回到「未测出」（推断值重新生效）。
    """
    if key not in CAPABILITY_KEYS:
        raise ValueError("未知的能力项：%s" % key)
    if value not in CAPABILITY_VALUES:
        raise ValueError("未知的取值：%s" % value)

    overrides = record.setdefault("model_overrides", {})
    entry = dict(overrides.get(model_id) or {})
    saved = dict(entry.get("capabilities") or {})
    manual = dict(saved.get("manual") or {})

    if value == "unknown":
        saved.pop(key, None)
        manual.pop(key, None)
    else:
        saved[key] = value
        manual[key] = True
    saved["verified_at"] = _today()
    saved["note"] = "手动指定"
    if manual:
        saved["manual"] = manual
    else:
        saved.pop("manual", None)
    _apply_manual_side_effects(entry, key, value)

    if saved.get("manual") or any(saved.get(k) for k in CAPABILITY_KEYS):
        entry["capabilities"] = saved
    else:
        entry.pop("capabilities", None)
    # 清空到没有任何覆盖项，就把整个条目删掉，别在状态里留空壳
    if not entry:
        overrides.pop(model_id, None)
    else:
        overrides[model_id] = entry
    return {"provider": provider_id, "model": model_id, "key": key, "value": value,
            "capabilities": saved}


# ------------------------------------------------------------------ 实测探针

# 读图探针用的两张图：红底上分别画 2 个和 3 个蓝色方块（160×120，压缩后不到 400 字节）。
#
# 为什么不是纯色图：一开始用的是 8×8 / 64×64 纯色图，MiniMax 直接返回
# HTTP 500 「system error (1033)」，纯色图被它的图片管线当成坏图了。
# 为什么是「数方块」而不是「什么颜色」：问颜色时模型蒙对的概率有一半，
# MiniMax-M3 就蒙对过一次，害我一度以为它支持读图、其实是图根本没传进去。
# 数方块 + 两张图都对，蒙对的概率降到百分之一。
_PROBE_PNG_2_SQUARES = (
    "iVBORw0KGgoAAAANSUhEUgAAAKAAAAB4CAIAAAD6wG44AAAA50lEQVR42u3RAQkAAAgDwfUvrSkE"
    "lYMPMHapRI9zAWABFmABFmABFmDAAizAAizAAizAgAVYgAVYu4GnJ13ZABgwYMCAAQMGDBgwYMCAA"
    "QMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBjwBWABFmABFm"
    "ABBuwCwAIswAIswAIswIAFWIAFWIAFWIABC7AAC7AAC7AACzBgARZgARZgARZgwAIswAIswAIswIAF"
    "WIAFWIAFWIAFGLAAC7AAC7AACzBgARZgARZgARZgwAIswFpaAy2iuVd3/C1iAAAAAElFTkSuQmCC"
)
_PROBE_PNG_3_SQUARES = (
    "iVBORw0KGgoAAAANSUhEUgAAAKAAAAB4CAIAAAD6wG44AAAA60lEQVR42u3RwQkAMAwDMe+/dLtC"
    "PoHiCm4AY+UkKs4FgAVYgAVYgAVYgAELsAALsAALsAADFmABFmC9Dbw9yYb5BsCAAQN2LmDAgAEDB"
    "gzYBsCAAQN2LmDAgAEDBgzYBsCAAQMGDNi5gAEDBgzYBsA2AAYMGLBzAQMGDBgwYMA2AAYMGLC6cg"
    "FgARZgARZgARZgwAIswAIswAIswIAFWIAFWIAFWIAFGLAAC7AAC7AACzBgARZgARZgARZgwAIswAI"
    "swAIswAIMWIAFWIAFWIAFGLAAC7AAC7AACzBgFwAWYAEWYAEWYAH+qAs1KrlXZaezQgAAAABJRU5E"
    "rkJggg=="
)

# (正确答案, 图片)。两张都对才算「看得见图」。
_VISION_CASES = (("2", _PROBE_PNG_2_SQUARES), ("3", _PROBE_PNG_3_SQUARES))
_VISION_QUESTION = ("How many blue squares are on this red image? "
                    "Answer with a single digit, nothing else.")

# 输出预算。想「省一点」只给 64 是错的：思考型模型会先把预算花在思考上，
# 真正留给答案的 token 就没了，最终回答被截断成空字符串 ——
# 这会让探针把「答不出来」误判成「看不见图」（DeepSeek 就被这样冤枉过一次）。
_VISION_BUDGET = 300
_REASON_BUDGET = 300
_TOOL_BUDGET = 200
_REASON_QUESTION = "房间里有3个人，进来2个人，又走了1个人，现在房间里有几个人？只回答一个数字。"
_TOOL_QUESTION = "用工具查一下北京的天气。"

_WEATHER_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "查询某个城市的天气",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
}


class ProbeError(RuntimeError):
    """探测失败，原因写成人话。"""


def _post(url: str, api_key: Optional[str], payload: Dict, timeout: int,
          extra_headers: Optional[Dict] = None) -> Tuple[Optional[Dict], str]:
    """发一个 JSON 请求。返回 (解析后的 JSON, 出错说明)。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "codex-model-switcher/1.0",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(400000).decode("utf-8", "replace")
        return json.loads(raw), ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(240).decode("utf-8", "replace").replace("\n", " ")
        except Exception:  # noqa: BLE001 - 读不到正文也要给出状态码
            pass
        return None, "HTTP %d %s" % (exc.code, detail[:180])
    except Exception as exc:  # noqa: BLE001 - 网络层错误一律转成说明
        return None, type(exc).__name__


def _responses_text(document: Dict) -> str:
    """从 Responses 响应里抠出文本。不同平台字段位置不完全一样。"""
    parts: List[str] = []
    for item in document.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _chat_text(document: Dict) -> str:
    choices = document.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "").strip()


def _reasoning_tokens(document: Dict) -> int:
    """各家把思考 token 放在不同地方，挨个找一遍。"""
    usage = document.get("usage") or {}
    buckets = [usage]
    for key in ("output_tokens_details", "completion_tokens_details", "reasoning_tokens_details"):
        if isinstance(usage.get(key), dict):
            buckets.append(usage[key])
    for bucket in buckets:
        for key in ("reasoning_tokens", "reasoning", "thinking_tokens"):
            value = bucket.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    return 0


def probe(provider_id: str, record: Dict, model_id: str, api_key: Optional[str],
          timeout: int = 30) -> Dict:
    """对一个模型做三项实测：读图 / 思考 / 工具调用。

    返回 {"vision": ..., "reasoning": ..., "tools": ..., "evidence": {...}}
    每一项只可能是 yes / no / unknown。
    """
    transport = "bridge" if (record.get("transport") or "") == "bridge" else "native"
    base = (record.get("base_url") or "").rstrip("/")
    if not base:
        raise ProbeError("这个平台没有记录 Base URL，没法探测")
    extra_headers = record.get("extra_headers") or {}
    result = {"model": model_id, "transport": transport, "verified_at": _today(),
              "vision": "unknown", "reasoning": "unknown", "tools": "unknown",
              "evidence": {}}

    if transport == "native":
        url = base + "/responses"
        _probe_native(url, api_key, model_id, timeout, extra_headers, result)
    else:
        url = base + "/chat/completions"
        _probe_chat(url, api_key, model_id, timeout, extra_headers, result,
                    thinking_payload(provider_id, "high"))
    return result


def _vision_responses_blocks(png_b64: str) -> List[List[Dict]]:
    """图片块的写法，按优先级排。

    首选 input_image（OpenAI 规范，也是 Codex 真正会发出的写法）。
    备用 image_url：少数平台只认这个。

    注意：只有在首选写法「报错」时才换备用，答错不换 ——
    曾经因为答错就换写法，把根本没收到图的回答当成了结论，
    误判 MiniMax-M3 读不了图（官方明确支持，实测也能答对数方块）。
    """
    url = "data:image/png;base64," + png_b64
    text = {"type": "input_text", "text": _VISION_QUESTION}
    return [
        [text, {"type": "input_image", "image_url": url}],
        [text, {"type": "image_url", "image_url": {"url": url}}],
    ]


def _vision_chat_blocks(png_b64: str) -> List[List[Dict]]:
    url = "data:image/png;base64," + png_b64
    text = {"type": "text", "text": _VISION_QUESTION}
    return [
        [text, {"type": "image_url", "image_url": {"url": url}}],
        [text, {"type": "input_image", "image_url": url}],
    ]


def _answers_count(text: str, expected: str) -> bool:
    """数方块的回答里有没有正确答案。

    只接受「答案里出现过这个数字」，但不接受同时出现别的数字
    （模型有时候会把推理过程一起吐出来）。
    """
    digits = [ch for ch in (text or "") if ch.isdigit()]
    return bool(digits) and set(digits) == {expected}


def _probe_vision(post, url, api_key, model_id, timeout, extra_headers,
                  result, block_builder, text_reader, input_key: str) -> None:
    """跑两张图，两张都答对才算支持读图。"""
    answers: List[str] = []
    failed = ""
    for expected, png_b64 in _VISION_CASES:
        answered = False
        empty = False
        for blocks in block_builder(png_b64):
            payload = {"model": model_id, "max_output_tokens": _VISION_BUDGET}
            if input_key == "input":
                payload["input"] = [{"role": "user", "content": blocks}]
            else:
                payload["messages"] = [{"role": "user", "content": blocks}]
            document, error = post(url, api_key, payload, timeout, extra_headers)
            if document is None:
                failed = error or "请求失败"
                continue  # 换一种写法再试
            answer = text_reader(document)
            if not answer.strip():
                # 一个字都没有，说明不了问题：可能是输出预算被思考吃光了，
                # 也可能是平台不接受这种图片块。换写法再试，别直接判「不支持」。
                empty = True
                failed = "返回空回答"
                continue
            answers.append(answer[:40])
            answered = True
            if not _answers_count(answer, expected):
                result["vision"] = "no"
                result["evidence"]["vision"] = (
                    "数方块题答错：%s（正确答案 %s）" % (answer[:40], expected))
                return
            break
        if not answered:
            result["evidence"]["vision"] = (
                ("空回答" if empty else "") + "，无法判定：" + failed).strip("，：")
            return
    if len(answers) == len(_VISION_CASES):
        result["vision"] = "yes"
        result["evidence"]["vision"] = "两张数方块图都答对：%s" % " / ".join(answers)
    else:
        result["evidence"]["vision"] = failed or "没能拿到完整结论"


def _probe_native(url, api_key, model_id, timeout, extra_headers, result) -> None:
    # ---- 读图
    _probe_vision(_post, url, api_key, model_id, timeout, extra_headers, result,
                  _vision_responses_blocks, _responses_text, "input")

    # ---- 思考
    document, error = _post(url, api_key, {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": _REASON_QUESTION}]}],
        "reasoning": {"effort": "high"},
        "max_output_tokens": _REASON_BUDGET,
    }, timeout, extra_headers)
    if document is None:
        result["evidence"]["reasoning"] = error or "请求失败"
    else:
        tokens = _reasoning_tokens(document)
        result["reasoning"] = "yes" if tokens > 0 else "unknown"
        result["evidence"]["reasoning"] = (
            "思考 token %d，回答=%s" % (tokens, _responses_text(document)[:40])
            if tokens else "平台未上报思考 token，无法判定")

    # ---- 工具调用
    document, error = _post(url, api_key, {
        "model": model_id,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": _TOOL_QUESTION}]}],
        "tools": [_WEATHER_TOOL],
        "tool_choice": "auto",
        "max_output_tokens": _TOOL_BUDGET,
    }, timeout, extra_headers)
    if document is None:
        result["evidence"]["tools"] = error or "请求失败"
    else:
        called = any(isinstance(item, dict) and item.get("type") == "function_call"
                     for item in document.get("output") or [])
        result["tools"] = "yes" if called else "no"
        result["evidence"]["tools"] = "发起了函数调用" if called else "没有返回函数调用"


def _probe_chat(url, api_key, model_id, timeout, extra_headers, result,
                thinking: Optional[Dict] = None) -> None:
    # ---- 读图
    _probe_vision(_post, url, api_key, model_id, timeout, extra_headers, result,
                  _vision_chat_blocks, _chat_text, "messages")

    # ---- 思考
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": _REASON_QUESTION}],
        "max_tokens": _REASON_BUDGET,
    }
    payload.update(thinking or {})
    document, error = _post(url, api_key, payload, timeout, extra_headers)
    if document is None:
        result["evidence"]["reasoning"] = error or "请求失败"
    else:
        tokens = _reasoning_tokens(document)
        result["reasoning"] = "yes" if tokens > 0 else "unknown"
        result["evidence"]["reasoning"] = (
            "思考 token %d，回答=%s" % (tokens, _chat_text(document)[:40])
            if tokens else "平台未上报思考 token，无法判定")

    # ---- 工具调用
    document, error = _post(url, api_key, {
        "model": model_id,
        "messages": [{"role": "user", "content": _TOOL_QUESTION}],
        "tools": [{"type": "function", "function": _WEATHER_TOOL}],
        "tool_choice": "auto",
        "max_tokens": _TOOL_BUDGET,
    }, timeout, extra_headers)
    if document is None:
        result["evidence"]["tools"] = error or "请求失败"
    else:
        choices = document.get("choices") or [{}]
        message = (choices[0] or {}).get("message") or {}
        called = bool(message.get("tool_calls"))
        result["tools"] = "yes" if called else "no"
        result["evidence"]["tools"] = "发起了函数调用" if called else "没有返回函数调用"


def apply_result(provider_id: str, record: Dict, model_id: str, result: Dict) -> Dict:
    """把实测结论写进平台记录。

    只改「实测有结论」的项：unknown 说明没测出来，不能当成 no 写进去。
    """
    overrides = record.setdefault("model_overrides", {})
    entry = dict(overrides.get(model_id) or {})
    saved = dict(entry.get("capabilities") or {})
    changed = []

    vision = result.get("vision")
    if vision in ("yes", "no"):
        entry["input_modalities"] = ["text", "image"] if vision == "yes" else ["text"]
        saved["vision"] = vision
        changed.append("读图")

    reasoning = result.get("reasoning")
    if reasoning == "yes":
        saved["reasoning"] = "yes"
        if not entry.get("default_reasoning_level"):
            entry["default_reasoning_level"] = "high"
        changed.append("思考")
    elif reasoning == "no":
        saved["reasoning"] = "no"
        entry["default_reasoning_level"] = "none"
        changed.append("思考")

    tools = result.get("tools")
    if tools in ("yes", "no"):
        saved["tools"] = tools
        entry["supports_parallel_tool_calls"] = (tools == "yes")
        changed.append("工具调用")

    if changed:
        saved["verified_at"] = result.get("verified_at") or _today()
        saved["note"] = "本机实测"
        entry["capabilities"] = saved
        overrides[model_id] = entry
    return {"changed": changed, "override": entry}


def _today() -> str:
    """用系统时钟取当天日期，不自己算——手算日期容易算错年份。"""
    return datetime.date.today().isoformat()
