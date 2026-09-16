"""本机用量统计。

平台不给额度时，至少能告诉你“这台机器上用了多少”。数据来自 Codex 自己的
会话日志（~/.codex/sessions），只统计 token 数与会话数，不读取对话内容。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, Optional

from . import paths

MAX_FILES = 300
MAX_AGE_DAYS = 120
CACHE_TTL_SECONDS = 60
# 只在文件开头找是哪个平台，在结尾找最后一次 token 统计。
# 早期版本整文件扫描，遇到 6MB 的会话文件会把界面拖到超时。
# session_meta 那一行很大（内嵌基础指令），必须留够空间；
# 万一还是被截断，下面还有正则兜底。
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024
# 一次统计最多花多久，超出就用手上已有的数据
SCAN_BUDGET_SECONDS = 4.0
_CACHE: Dict[str, object] = {"at": 0.0, "value": {}}
_PROVIDER_RE = re.compile(r'"model_provider"\s*:\s*"([^"\\]+)"')


def _scan_file(path: Path) -> Optional[Dict]:
    provider = None
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    cached = 0
    turns = 0
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(HEAD_BYTES).decode("utf-8", "replace")
        for line in head.splitlines():
            if '"model_provider"' not in line:
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError:
                # 行被截断也没关系，直接抠字段
                match = _PROVIDER_RE.search(line)
                if match:
                    provider = match.group(1)
                    break
                continue
            payload = document.get("payload") or {}
            if document.get("type") == "session_meta" and payload.get("model_provider"):
                provider = payload["model_provider"]
                break
        if provider is None:
            match = _PROVIDER_RE.search(head)
            if match:
                provider = match.group(1)
        if provider is None:
            return None

        start = max(0, size - TAIL_BYTES)
        with path.open("rb") as stream:
            stream.seek(start)
            if start:
                stream.readline()  # 丢掉可能被截断的半行
            tail = stream.read().decode("utf-8", "replace")
        for line in tail.splitlines():
            if '"turn_context"' in line:
                turns += 1
            if '"token_count"' not in line:
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
    deadline = time.time() + SCAN_BUDGET_SECONDS
    for _, _, path in candidates[:max_files]:
        if time.time() > deadline:
            break
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
