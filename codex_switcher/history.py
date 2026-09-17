"""会话历史的协议兼容性检查与清洗。

为什么需要这个东西
------------------
Codex 会把整段会话历史原样回放到下一次请求里。历史里有几类条目是
**只有官方 OpenAI 才认识、或者干脆是坏的**，一旦切到第三方平台就会炸：

1. ``function_call_output`` 缺 ``call_id``
   官方文档把「Sending a function result without the matching call_id」
   列为迁移常见错误。服务端会直接 400：
       Failed to deserialize the JSON body into the target type:
       input: missing field `call_id`
   实测用户机器上 1089 条 function_call_output 里有 15 条是这种孤儿记录，
   来源是 Codex App 自带的工具（codex_app 命名空间），它们只写了输出没写调用。

2. ``reasoning`` 带 ``encrypted_content``
   这是 OpenAI 专有的加密推理状态，官方说明它的用途就是"在无状态调用之间
   复用推理"。换到第三方平台之后它没有任何意义，而且会被当成未知字段。

3. 其它 OpenAI 专有条目类型（``custom_tool_call`` / ``web_search_call`` 等）
   这里只报告，不擅自删改 —— 它们承载真实工具调用，删了会丢上下文。

清洗原则：只删"确定是坏的"和"确定对方用不上"的，其余一律只报告。
改之前先备份，改完逐行校验仍是合法 JSON。
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths

BACKUP_DIRNAME = "history-backups"

# 最近还在写入的文件不要碰：那多半是你正开着的那个对话。
# 改写一个正在被追加写的文件，可能把当前对话写坏。等它静下来再清。
ACTIVE_GUARD_SECONDS = 120

# 第三方平台不认识、且删掉不影响语义的条目
OPENAI_ONLY_IF_MOVING = {"reasoning"}
# 只报告、不自动处理的 OpenAI 专有类型
REPORT_ONLY_TYPES = {"custom_tool_call", "custom_tool_call_output", "web_search_call",
                     "computer_call", "computer_call_output", "file_search_call",
                     "code_interpreter_call", "image_generation_call"}


def _payload_of(line: str) -> Optional[Dict]:
    """只解析需要的那一行；解不动就返回 None（不抛）。"""
    if '"response_item"' not in line:
        return None
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def inspect(path: Path) -> Dict:
    """看一个 rollout 文件里有没有问题条目。只读，不改。"""
    result = {"path": str(path), "orphan_outputs": 0, "openai_only": 0,
              "report_only": {}, "lines": 0, "error": None}
    if not path.exists():
        result["error"] = "文件不存在"
        return result
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    lines = text.split("\n")
    result["lines"] = len(lines)
    for line in lines:
        if not line or "response_item" not in line:
            continue
        payload = _payload_of(line)
        if payload is None:
            continue
        kind = payload.get("type")
        if kind == "function_call_output" and not payload.get("call_id"):
            result["orphan_outputs"] += 1
        elif kind in OPENAI_ONLY_IF_MOVING and payload.get("encrypted_content"):
            result["openai_only"] += 1
        elif kind in REPORT_ONLY_TYPES:
            result["report_only"][kind] = result["report_only"].get(kind, 0) + 1
    return result


def is_dirty(info: Dict) -> bool:
    return bool(info.get("orphan_outputs") or info.get("openai_only"))


def _backup_root() -> Path:
    return paths.state_dir() / BACKUP_DIRNAME


def sanitize(path: Path, moving_off_openai: bool, backup_dir: Path) -> Tuple[bool, Dict]:
    """删掉指定文件里的问题条目。返回 (是否改动, 统计)。"""
    stats = {"removed_orphan_outputs": 0, "removed_openai_only": 0, "kept": 0,
             "backup": None}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, stats

    lines = text.split("\n")
    kept: List[str] = []
    for line in lines:
        payload = _payload_of(line) if ('function_call_output' in line
                                        or 'encrypted_content' in line) else None
        if payload is not None:
            kind = payload.get("type")
            if kind == "function_call_output" and not payload.get("call_id"):
                stats["removed_orphan_outputs"] += 1
                continue
            if (moving_off_openai and kind in OPENAI_ONLY_IF_MOVING
                    and payload.get("encrypted_content")):
                stats["removed_openai_only"] += 1
                continue
        if line:
            kept.append(line)
    stats["kept"] = len(kept)

    if not stats["removed_orphan_outputs"] and not stats["removed_openai_only"]:
        return False, stats

    # 备份：保留原文件，文件名带时间戳，放工具自己的状态目录里
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        relative = path.relative_to(paths.codex_home())
    except ValueError:
        relative = Path(path.name)
    target = backup_dir / (stamp + "-" + relative.name)
    target.write_text(text, encoding="utf-8")
    stats["backup"] = str(target)

    # 逐行校验后再落盘：写回去的每一行都必须是合法 JSON
    out = "\n".join(kept)
    for line in kept:
        if line.strip():
            try:
                json.loads(line)
            except (json.JSONDecodeError, ValueError):
                return False, stats          # 校验不过就整单放弃，不写盘
    if text.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    path.write_text(out, encoding="utf-8")
    return True, stats


def recent_rollouts(limit: int = 30) -> List[Path]:
    """按修改时间取最近的若干会话文件。全量有 30GB+，所以默认只看最近的。"""
    root = paths.sessions_dir()
    if not root.exists():
        return []
    files = list(root.rglob("rollout-*.jsonl"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit] if limit > 0 else files


def scan(limit: int = 30) -> Dict:
    """扫最近的会话，汇总问题条目。"""
    report = {"scanned": 0, "dirty": [], "totals": {"orphan_outputs": 0, "openai_only": 0}}
    for path in recent_rollouts(limit):
        report["scanned"] += 1
        info = inspect(path)
        if is_dirty(info):
            report["dirty"].append(info)
            report["totals"]["orphan_outputs"] += info["orphan_outputs"]
            report["totals"]["openai_only"] += info["openai_only"]
    return report


def clean(paths_to_clean: List[Path], moving_off_openai: bool,
          dry_run: bool = False) -> Dict:
    """清洗给定的会话文件。dry_run 只报告不改。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _backup_root() / stamp
    report = {"changed": 0, "dry_run": dry_run, "moved_off_openai": moving_off_openai,
              "items": [], "backup_dir": None, "skipped_active": [],
              "removed": {"orphan_outputs": 0, "openai_only": 0}}
    for path in paths_to_clean:
        info = inspect(path)
        if not is_dirty(info):
            continue
        try:
            if time.time() - path.stat().st_mtime < ACTIVE_GUARD_SECONDS:
                report["skipped_active"].append(str(path))
                continue
        except OSError:
            pass
        if dry_run:
            report["items"].append({"path": str(path), "would_remove": {
                "orphan_outputs": info["orphan_outputs"],
                "openai_only": info["openai_only"] if moving_off_openai else 0}})
            report["changed"] += 1
            continue
        changed, stats = sanitize(path, moving_off_openai, backup_dir)
        if changed:
            report["changed"] += 1
            report["backup_dir"] = str(backup_dir)
            report["removed"]["orphan_outputs"] += stats["removed_orphan_outputs"]
            report["removed"]["openai_only"] += stats["removed_openai_only"]
            report["items"].append({"path": str(path), "removed": {
                "orphan_outputs": stats["removed_orphan_outputs"],
                "openai_only": stats["removed_openai_only"]}})
    return report
