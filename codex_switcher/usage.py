"""本机用量统计。

平台不给额度时，至少能告诉你“这台机器上用了多少”。数据来自 Codex 自己的
会话日志（~/.codex/sessions），只统计 token 数与会话数，不读取对话内容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Optional

from . import paths

MAX_FILES = 300
MAX_AGE_DAYS = 120
CACHE_TTL_SECONDS = 60
_CACHE: Dict[str, object] = {"at": 0.0, "value": {}}


def _scan_file(path: Path) -> Optional[Dict]:
    provider = None
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    cached = 0
    turns = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"model_provider"' in line:
                    try:
                        document = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = document.get("payload") or {}
                    if document.get("type") == "session_meta" and payload.get("model_provider"):
                        provider = payload["model_provider"]
                    continue
                if '"token_count"' not in line:
                    if '"turn_context"' in line:
                        turns += 1
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = document.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                usage = ((payload.get("info") or {}).get("total_token_usage")) or {}
                total_tokens = max(total_tokens, int(usage.get("total_tokens") or 0))
                input_tokens = max(input_tokens, int(usage.get("input_tokens") or 0))
                output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
                cached = max(cached, int(usage.get("cached_input_tokens") or 0))
    except OSError:
        return None
    if provider is None:
        return None
    return {
        "provider": provider,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached,
        "turns": turns,
        "file": str(path),
    }


def local_usage(max_files: int = MAX_FILES, max_age_days: int = MAX_AGE_DAYS) -> Dict:
    """返回 {provider_id: {...汇总...}}，按最近修改的会话优先。"""
    now = time.time()
    if _CACHE.get("value") and now - float(_CACHE.get("at") or 0) < CACHE_TTL_SECONDS:
        return dict(_CACHE["value"])  # type: ignore[arg-type]
    root = paths.sessions_dir()
    if not root.exists():
        return {}
    cutoff = time.time() - max_age_days * 86400
    candidates = []
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        candidates.append((stat.st_mtime, stat.st_size, path))
    candidates.sort(reverse=True)
    summary: Dict[str, Dict] = {}
    for _, _, path in candidates[:max_files]:
        record = _scan_file(path)
        if not record:
            continue
        bucket = summary.setdefault(record["provider"], {
            "sessions": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "turns": 0,
        })
        bucket["sessions"] += 1
        bucket["total_tokens"] += record["total_tokens"]
        bucket["input_tokens"] += record["input_tokens"]
        bucket["output_tokens"] += record["output_tokens"]
        bucket["cached_input_tokens"] += record["cached_input_tokens"]
        bucket["turns"] += record["turns"]
    _CACHE["at"] = now
    _CACHE["value"] = summary
    return summary


def human_tokens(value: int) -> str:
    number = float(value or 0)
    for unit in ("", "K", "M", "B"):
        if abs(number) < 1000 or unit == "B":
            if unit == "":
                return "%d" % number
            return ("%.1f%s" % (number, unit)).replace(".0", "")
        number /= 1000.0
    return str(value)
