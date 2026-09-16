"""余额 / 额度查询。

原则：只显示平台真正返回的数字。平台没开放额度接口时，明确显示“未开放”，
并给出官网入口，绝不用估算值冒充实测余额。
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request
from typing import Dict, Optional


def _http_json(url: str, api_key: Optional[str], headers: Optional[Dict] = None, timeout: int = 20):
    request_headers = {"Accept": "application/json", "User-Agent": "codex-model-switcher/1.0"}
    if api_key:
        request_headers["Authorization"] = "Bearer " + api_key
    if headers:
        request_headers.update({k: v for k, v in headers.items() if v})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(200000).decode("utf-8", "replace"))


def _dig(document, path: str):
    """按 "a.b.0.c" 取值；取不到返回 None。"""
    current = document
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _money(value, currency: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = ("%.2f" % number).rstrip("0").rstrip(".")
    return ("%s %s" % (text, currency)).strip()


# ------------------------------------------------------------------ 内置适配器

def _adapter_deepseek(record: Dict, api_key: Optional[str]) -> Dict:
    base = (record.get("base_url") or "https://api.deepseek.com").rstrip("/")
    document = _http_json(base + "/user/balance", api_key)
    infos = document.get("balance_infos") or []
    if not infos:
        return {"status": "error", "message": "接口没有返回余额字段"}
    info = infos[0]
    currency = info.get("currency", "CNY")
    return {
        "status": "ok",
        "display": _money(info.get("total_balance"), currency),
        "fields": [
            {"label": "总余额", "value": _money(info.get("total_balance"), currency)},
            {"label": "充值余额", "value": _money(info.get("topped_up_balance"), currency)},
            {"label": "赠送余额", "value": _money(info.get("granted_balance"), currency)},
            {"label": "账户可用", "value": "是" if document.get("is_available") else "否"},
        ],
    }


def _adapter_moonshot(record: Dict, api_key: Optional[str]) -> Dict:
    document = _http_json("https://api.moonshot.cn/v1/users/me/balance", api_key)
    data = document.get("data") or {}
    if not data:
        return {"status": "error", "message": "接口没有返回余额字段"}
    return {
        "status": "ok",
        "display": _money(data.get("available_balance"), "CNY"),
        "fields": [
            {"label": "可用余额", "value": _money(data.get("available_balance"), "CNY")},
            {"label": "代金券", "value": _money(data.get("voucher_balance"), "CNY")},
            {"label": "现金余额", "value": _money(data.get("cash_balance"), "CNY")},
        ],
    }


def _adapter_siliconflow(record: Dict, api_key: Optional[str]) -> Dict:
    document = _http_json("https://api.siliconflow.cn/v1/user/info", api_key)
    data = document.get("data") or {}
    if not data:
        return {"status": "error", "message": "接口没有返回账户信息"}
    return {
        "status": "ok",
        "display": _money(data.get("totalBalance"), "CNY"),
        "fields": [
            {"label": "总余额", "value": _money(data.get("totalBalance"), "CNY")},
            {"label": "可用余额", "value": _money(data.get("balance"), "CNY")},
            {"label": "充值余额", "value": _money(data.get("chargeBalance"), "CNY")},
        ],
    }


def _adapter_openrouter(record: Dict, api_key: Optional[str]) -> Dict:
    document = _http_json("https://openrouter.ai/api/v1/key", api_key)
    data = document.get("data") or {}
    if not data:
        return {"status": "error", "message": "接口没有返回密钥信息"}
    limit = data.get("limit")
    usage = data.get("usage")
    remaining = data.get("limit_remaining")
    fields = [
        {"label": "已用", "value": _money(usage, "USD")},
    ]
    if limit is not None:
        fields.append({"label": "额度上限", "value": _money(limit, "USD")})
    if remaining is not None:
        fields.append({"label": "剩余", "value": _money(remaining, "USD")})
    else:
        fields.append({"label": "剩余", "value": "按量付费，无硬上限"})
    display = _money(remaining, "USD") if remaining is not None else "按量付费"
    return {"status": "ok", "display": display, "fields": fields}


BUILTIN_ADAPTERS = {
    "deepseek": _adapter_deepseek,
    "moonshot": _adapter_moonshot,
    "siliconflow": _adapter_siliconflow,
    "openrouter": _adapter_openrouter,
}


# ---------------------------------------------------------------------- 入口

def query(record: Dict, api_key: Optional[str] = None, timeout: int = 20) -> Dict:
    """查询某个平台的额度。任何异常都会被翻译成给人看的结果。"""
    spec = record.get("balance")
    console_url = record.get("console_url")
    base = {"provider_id": record.get("id"), "console_url": console_url,
            "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}

    if not spec:
        return dict(base, status="unsupported", display="未开放接口",
                    message="该平台没有公开的余额/额度查询接口，请在官网查看。")

    if spec.get("kind") == "builtin":
        adapter = BUILTIN_ADAPTERS.get(spec.get("adapter"))
        if adapter is None:
            return dict(base, status="unsupported", display="不支持", message="未实现的适配器。")
        function = adapter
    elif spec.get("kind") == "json_path":
        def function(rec, key):  # type: ignore[misc]
            document = _http_json(spec["url"], key, timeout=timeout)
            value = _dig(document, spec.get("value_path", ""))
            if value is None:
                return {"status": "error", "message": "在返回内容里找不到约定的字段，请核对取值路径。"}
            currency = _dig(document, spec.get("currency_path", "")) or spec.get("currency", "")
            return {
                "status": "ok",
                "display": _money(value, currency),
                "fields": [{"label": spec.get("label", "余额"), "value": _money(value, currency)}],
            }
    else:
        return dict(base, status="unsupported", display="未开放接口", message="该平台未配置额度接口。")

    if record.get("requires_key", True) and not api_key:
        return dict(base, status="error", display="缺少密钥", message="没有找到该平台的密钥，请重新添加。")

    try:
        result = function(record, api_key)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            message = "密钥无效或无权查询额度（HTTP %d）" % exc.code
        elif exc.code == 404:
            message = "平台的额度接口地址有变化（HTTP 404）"
        else:
            message = "平台返回 HTTP %d" % exc.code
        return dict(base, status="error", display="查询失败", message=message)
    except urllib.error.URLError as exc:
        return dict(base, status="error", display="网络失败", message="无法连接：%s" % (exc.reason,))
    except Exception as exc:  # noqa: BLE001
        return dict(base, status="error", display="查询失败", message="请求出错：%s" % type(exc).__name__)

    return dict(base, **result)
