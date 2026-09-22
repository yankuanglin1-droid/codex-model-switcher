"""自动发现平台上的模型清单。

只做一件事：向平台的模型列表接口要一份清单，解析成字符串数组。
拿不到就明确报错原因，让用户改用手动填写，而不是悄悄返回空列表。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple


class DiscoveryError(RuntimeError):
    """带上人话解释的失败。"""


def _request(url: str, api_key: Optional[str], timeout: int, extra_headers: Optional[Dict] = None):
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-model-switcher/1.0",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def _extract_ids(document) -> List[str]:
    """兼容 OpenAI、Anthropic、部分中转站等几种返回结构。"""
    items = None
    if isinstance(document, list):
        items = document
    elif isinstance(document, dict):
        for key in ("data", "models", "result", "items"):
            if isinstance(document.get(key), list):
                items = document[key]
                break
    if items is None:
        return []
    ids: List[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            for key in ("id", "name", "model", "slug"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    ids.append(value.strip())
                    break
    return ids


def fetch_models(
    models_url: str,
    api_key: Optional[str] = None,
    timeout: int = 20,
    extra_headers: Optional[Dict] = None,
) -> Tuple[List[str], Dict]:
    """返回 (模型 ID 列表, 元信息)。失败抛 DiscoveryError。"""
    url = (models_url or "").strip()
    if not url:
        raise DiscoveryError("没有配置模型清单地址")
    try:
        with _request(url, api_key, timeout, extra_headers) as response:
            raw = response.read(4 * 1024 * 1024).decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
        except Exception:
            detail = ""
        if exc.code in (401, 403):
            raise DiscoveryError("密钥无效或没有读取模型列表的权限（HTTP %d）" % exc.code)
        if exc.code == 404:
            raise DiscoveryError("该平台没有这个模型列表接口（HTTP 404），请改用「手动添加模型」")
        if exc.code == 429:
            raise DiscoveryError("请求过于频繁，请稍后重试（HTTP 429）")
        raise DiscoveryError("平台返回 HTTP %d %s" % (exc.code, detail[:160]))
    except urllib.error.URLError as exc:
        raise DiscoveryError("网络无法连接：%s" % (exc.reason,))
    except Exception as exc:  # noqa: BLE001 - 转成人话
        raise DiscoveryError("请求失败：%s" % (type(exc).__name__,))

    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        raise DiscoveryError("平台返回的不是 JSON，无法解析模型列表")

    ids = [item for item in _extract_ids(document) if item]
    seen = set()
    unique = [item for item in ids if not (item in seen or seen.add(item))]
    return unique, {"url": url, "http_status": status, "count": len(unique)}


def rank_models(model_ids: List[str]) -> List[str]:
    """把看起来更新的型号排前面，方便在 Codex 的列表里先看到主力模型。

    规则：版本号大的优先，其次带 pro/max/turbo 的优先，最后按名字稳定排序。
    """
    def version_key(name: str):
        numbers = [int(part) for part in re.findall(r"\d+", name)]
        padded = (numbers + [0, 0, 0])[:3]
        return padded

    def score(name: str):
        lowered = name.lower()
        boost = 0
        for word, weight in (("pro", 3), ("max", 2), ("turbo", 2), ("latest", 4), ("fast", 1)):
            if word in lowered:
                boost += weight
        penalty = 1 if "highspeed" in lowered or "mini" in lowered or "air" in lowered else 0
        version = version_key(name)
        return (
            -(version[0] * 1000000 + version[1] * 10000 + version[2]),
            -(boost - penalty),
            name.lower(),
        )

    return sorted(model_ids, key=score)


def probe_chat(base_url: str, api_key: Optional[str], model_id: str, timeout: int = 30) -> Dict:
    """发一条最小请求，确认这个模型真的能跑通。"""
    endpoint = (base_url or "").rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "codex-model-switcher/1.0",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read(200000).decode("utf-8", "replace"))
        returned = ""
        if isinstance(document, dict):
            returned = document.get("model") or (document.get("choices") or [{}])[0].get("model", "")
        return {"ok": True, "returned_model": returned}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(240).decode("utf-8", "replace").replace("\n", " ")
        except Exception:
            pass
        return {"ok": False, "http_status": exc.code, "detail": body[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": type(exc).__name__}


def probe_responses(base_url: str, api_key: Optional[str] = None,
                    model_id: Optional[str] = None, timeout: int = 25) -> Dict:
    """探测平台是否自带 Responses 接口。

    返回 {"transport": "native" | "bridge" | "unknown", "evidence": ...}

    只有成功响应能被验证为 Responses 对象时才判为 ``native``。HTTP 400、
    422、429 只能证明某个网关接到了请求，不能证明它能承载 Codex 的完整
    历史、推理项和工具调用；把这些状态当作 native 会把不兼容的原始历史
    直送第三方，从而污染后续续接。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {"transport": "unknown", "evidence": "没有地址"}
    url = base + "/responses"
    payload = {
        "model": model_id or "probe",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "stream": False,
        "max_output_tokens": 8,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "codex-model-switcher/1.0"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(200000).decode("utf-8", "replace")
            try:
                document = json.loads(raw)
            except json.JSONDecodeError:
                return {"transport": "unknown", "evidence": "HTTP %d non-JSON" % response.status,
                        "verified_native": False}
            # A Responses result is an object with a typed output array.  A
            # generic JSON 200, proxy page, or chat-completions envelope is
            # not sufficient evidence for raw Codex transport.
            if isinstance(document, dict) and isinstance(document.get("output"), list):
                return {"transport": "native", "evidence": "verified Responses HTTP %d" % response.status,
                        "verified_native": True}
            return {"transport": "unknown", "evidence": "HTTP %d incompatible JSON" % response.status,
                    "verified_native": False}
    except urllib.error.HTTPError as exc:
        try:
            exc.read(400)
        except Exception:
            pass
        if exc.code in (404, 405):
            return {"transport": "bridge", "evidence": "HTTP %d" % exc.code,
                    "verified_native": False}
        if exc.code in (400, 422, 429):
            return {"transport": "unknown", "evidence": "HTTP %d (unverified)" % exc.code,
                    "verified_native": False}
        if exc.code in (401, 403):
            return {"transport": "unknown", "evidence": "HTTP %d" % exc.code,
                    "verified_native": False}
        return {"transport": "unknown", "evidence": "HTTP %d" % exc.code,
                "verified_native": False}
    except Exception as exc:  # noqa: BLE001
        return {"transport": "unknown", "evidence": type(exc).__name__,
                "verified_native": False}
