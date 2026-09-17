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
   各家第三方平台对这些条目都有自己的严格 schema（实测 deepseek 的
   web_search_call 要 ``queries`` 字段，MiniMax 产出的只有 ``action``），
   跨平台回放必然 400。补字段救不了，只能成对剥离：
   手动走 ``--cross-provider``，切换平台时由 :func:`auto_clean` 自动做。

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
# OpenAI 服务端工具留下的条目。它们由 OpenAI 那边执行（网页搜索、看图生图、
# 用电脑、代码解释器……），第三方平台既没有这些工具，也不认识这些条目类型，
# 整个请求会被直接拒掉。
#
# 迁移到别的平台时**必须成对剥离**：call 和 output 一起删。
# 只删一半会在历史里留下悬空引用，比留着还糟（实测过）。
#
# 默认情况下我们只报告不删 —— 它们承载真实工具调用，留在官方平台上是有意义的。
# 只有明确「要搬到别的平台」时才删（--cross-provider）。
CROSS_PROVIDER_TYPES = {"custom_tool_call", "custom_tool_call_output", "web_search_call",
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
              "cross_provider": 0, "report_only": {}, "lines": 0, "error": None}
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
        elif kind in CROSS_PROVIDER_TYPES:
            result["cross_provider"] += 1
            result["report_only"][kind] = result["report_only"].get(kind, 0) + 1
    return result


def is_dirty(info: Dict, cross_provider: bool = False) -> bool:
    """有没有需要清理的东西。

    cross_provider=False 时只看「确定是坏的」两类；
    要搬去别的平台时，OpenAI 专有条目也得算进去。
    """
    dirty = bool(info.get("orphan_outputs") or info.get("openai_only"))
    if cross_provider:
        dirty = dirty or bool(info.get("cross_provider"))
    return dirty


def _backup_root() -> Path:
    return paths.state_dir() / BACKUP_DIRNAME


def sanitize(path: Path, moving_off_openai: bool, backup_dir: Path,
             cross_provider: bool = False) -> Tuple[bool, Dict]:
    """删掉指定文件里的问题条目。返回 (是否改动, 统计)。

    cross_provider=True 时额外剥离 OpenAI 服务端工具的成对条目，
    这样这个会话才能搬到第三方平台上继续。
    """
    stats = {"removed_orphan_outputs": 0, "removed_openai_only": 0,
             "removed_cross_provider": 0, "kept": 0, "backup": None}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, stats

    lines = text.split("\n")
    kept: List[str] = []
    for line in lines:
        payload = _payload_of(line) if cross_provider else (
            _payload_of(line) if ('function_call_output' in line
                                  or 'encrypted_content' in line) else None)
        if payload is not None:
            kind = payload.get("type")
            if kind == "function_call_output" and not payload.get("call_id"):
                stats["removed_orphan_outputs"] += 1
                continue
            if (moving_off_openai and kind in OPENAI_ONLY_IF_MOVING
                    and payload.get("encrypted_content")):
                stats["removed_openai_only"] += 1
                continue
            if cross_provider and kind in CROSS_PROVIDER_TYPES:
                # call 与 output 都走这一支，所以是成对删，不会留悬空引用
                stats["removed_cross_provider"] += 1
                continue
        if line:
            kept.append(line)
    stats["kept"] = len(kept)

    if not (stats["removed_orphan_outputs"] or stats["removed_openai_only"]
            or stats["removed_cross_provider"]):
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


def scan(limit: int = 30, cross_provider: bool = False) -> Dict:
    """扫最近的会话，汇总问题条目。"""
    report = {"scanned": 0, "dirty": [],
              "totals": {"orphan_outputs": 0, "openai_only": 0, "cross_provider": 0}}
    for path in recent_rollouts(limit):
        report["scanned"] += 1
        info = inspect(path)
        if is_dirty(info, cross_provider=cross_provider):
            report["dirty"].append(info)
            report["totals"]["orphan_outputs"] += info["orphan_outputs"]
            report["totals"]["openai_only"] += info["openai_only"]
            report["totals"]["cross_provider"] += info["cross_provider"]
    return report


def clean(paths_to_clean: List[Path], moving_off_openai: bool,
          dry_run: bool = False, cross_provider: bool = False) -> Dict:
    """清洗给定的会话文件。dry_run 只报告不改。

    cross_provider=True 用于「要搬到第三方平台继续」：额外剥离 OpenAI
    服务端工具的成对条目。默认不开，因为那些条目在官方平台上是有效的。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _backup_root() / stamp
    report = {"changed": 0, "dry_run": dry_run, "moved_off_openai": moving_off_openai,
              "cross_provider": cross_provider,
              "items": [], "backup_dir": None, "skipped_active": [],
              "removed": {"orphan_outputs": 0, "openai_only": 0, "cross_provider": 0}}
    for path in paths_to_clean:
        info = inspect(path)
        if not is_dirty(info, cross_provider=cross_provider):
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
                "openai_only": info["openai_only"] if moving_off_openai else 0,
                "cross_provider": info["cross_provider"] if cross_provider else 0}})
            report["changed"] += 1
            continue
        changed, stats = sanitize(path, moving_off_openai, backup_dir,
                                  cross_provider=cross_provider)
        if changed:
            report["changed"] += 1
            report["backup_dir"] = str(backup_dir)
            report["removed"]["orphan_outputs"] += stats["removed_orphan_outputs"]
            report["removed"]["openai_only"] += stats["removed_openai_only"]
            report["removed"]["cross_provider"] += stats["removed_cross_provider"]
            report["items"].append({"path": str(path), "removed": {
                "orphan_outputs": stats["removed_orphan_outputs"],
                "openai_only": stats["removed_openai_only"],
                "cross_provider": stats["removed_cross_provider"]}})
    return report


# --------------------------------------------------------------- 切换时自动清洗

# 注意分工（2026-09-17 起）：
#   · 正确性由 threads._rewrite_session_file 保证 —— 任何改绑路径
#     （follow_switch / repair）都在同一次改写里剥掉跨平台条目，
#     不存在「只改绑、不清洗」的路径；
#   · 本函数是兜底扫描：清掉这个机制上线之前就已经被搬过、历史上还挂着
#     别家条目的存量文件，以及没有任何改绑动作时的漏网之鱼。
#
# 为什么需要它：
#   实测（2026-09-17）：MiniMax 的 Responses API 能真的执行 web_search，
#   产出的 web_search_call 条目只有 id 没有 call_id；deepseek 对同一类型
#   有自己的严格 schema（要 queries 字段）。跨平台回放历史时谁也不认谁，
#   全部 400（missing field call_id / queries …）。
#   补字段救不了（各家 schema 不一样），唯一可靠的办法是切换平台时
#   把别家产生的服务端工具条目剥掉 —— 也就是 cross_provider 清洗。
#   但这件事以前要手动跑 `history --clean --cross-provider`，用户不会记得。
#   所以 switch_to 切换成功后自动跑一遍最近的会话。

LEDGER_NAME = "history-autoclean.json"
AUTO_CLEAN_LIMIT = 30          # 每次最多处理最近多少份会话
AUTO_CLEAN_BUDGET_SECONDS = 10.0  # 总时间预算：巨文件拖不慢切换，剩下的下次接着清


def _ledger_path() -> Path:
    return paths.state_dir() / LEDGER_NAME


def _load_ledger() -> Dict:
    try:
        data = json.loads(_ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_ledger(ledger: Dict) -> None:
    try:
        paths.ensure_dir(paths.state_dir())
        _ledger_path().write_text(
            json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 账本写不进去只是下次多扫一遍，不影响功能


def auto_clean(moving_off_openai: bool, limit: int = AUTO_CLEAN_LIMIT,
               budget_seconds: float = AUTO_CLEAN_BUDGET_SECONDS) -> Dict:
    """切换平台后自动清洗最近的会话（cross_provider 模式，先备份）。

    带两层保护：
      · 账本缓存：已经清过且没再改动的文件直接跳过，切换保持秒回；
      · 时间预算：从最新的文件开始处理，超时就收工，剩余的下次切换接着清
        （最新的会话最可能被 resume，所以优先级最高）。
    任何异常都不能影响切换本身，调用方兜底即可（这里也不再抛）。
    """
    started = time.time()
    report = {"cleaned": 0, "skipped_active": 0, "checked": 0,
              "budget_exhausted": False,
              "removed": {"orphan_outputs": 0, "openai_only": 0,
                          "cross_provider": 0},
              "backup_dir": None}
    ledger = _load_ledger()
    try:
        rollouts = recent_rollouts(limit)
    except OSError:
        return report
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for path in rollouts:
        if time.time() - started > budget_seconds:
            report["budget_exhausted"] = True
            break
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        # 清过且没再动过：账本命中，直接跳过
        if ledger.get(key) == mtime:
            continue
        # 正在写的会话不碰（同 sanitize 的保护逻辑）
        if time.time() - mtime < ACTIVE_GUARD_SECONDS:
            report["skipped_active"] += 1
            continue
        info = inspect(path)
        report["checked"] += 1
        if is_dirty(info, cross_provider=True):
            changed, stats = sanitize(path, moving_off_openai,
                                      _backup_root() / stamp,
                                      cross_provider=True)
            if changed:
                report["cleaned"] += 1
                report["backup_dir"] = str(_backup_root() / stamp)
                for field in report["removed"]:
                    report["removed"][field] += stats.get("removed_" + field, 0)
        # 不管改没改，这份文件此刻是干净的，记账本（含只检查没改动的）
        ledger[key] = mtime
    _save_ledger(ledger)
    return report
