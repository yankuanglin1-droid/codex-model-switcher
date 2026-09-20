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
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths

BACKUP_DIRNAME = "history-backups"

# 备份的保留上限。这是个必须有的硬约束：清洗发生在每次切换、后台巡检和
# 定时任务里，而会话文件动辄几百 MB —— 上限一松，备份几天就能吃掉上百 GB
# （实测 9/16-9/18 两天攒了 9.9 GB，且此前没有任何清理机制，只涨不消）。
# 备份的价值是"清洗出错时能还原"，不是永久归档；原始会话文件本体始终在
# sessions/ 里，所以到期的备份可以放心删。
BACKUP_MAX_BYTES = int(os.environ.get(
    "CODEX_SWITCHER_HISTORY_BACKUP_MAX_GB", "3")) * 1024 * 1024 * 1024
BACKUP_MAX_AGE_DAYS = float(os.environ.get(
    "CODEX_SWITCHER_HISTORY_BACKUP_MAX_DAYS", "30"))

# 最近还在写入的文件不要碰：那多半是你正开着的那个对话。
# 改写一个正在被追加写的文件，可能把当前对话写坏。等它静下来再清。
ACTIVE_GUARD_SECONDS = 120

# 只靠 mtime 判断"这个文件还能不能动"是不够准的：实测 Codex 会把一个会话
# 文件的句柄一直攥在手里（fd 常驻），而那个对话可能几小时没动静 —— mtime
# 早就不新鲜了，文件却随时会被追加写。这时改写它（os.replace 换掉 inode）
# 会让 Codex 后续的写入落进已经没人引用的旧 inode，整段对话凭空消失。
# 所以先拿 lsof 问一句"Codex 现在到底攥着哪些文件"，比猜 mtime 准得多；
# 探不到 lsof 时再退回 mtime 那道保守护栏。
LSOF_TIMEOUT_SECONDS = 5.0
OPEN_FILES_CACHE_SECONDS = 5.0
# lsof 探针熔断：系统负载高时 lsof -c codex 可能 30s+ 才返回，每次等 5s 超时
# 会把清扫的时间预算全部烧掉（每个文件卡 5s、25s 预算只够 5 个文件）。
# 超时一次就本进程内熔断，之后直接退回 mtime 保守护栏——慢机器上宁可保守，
# 也不能把「全量清扫」拖成「只扫了 5 个」。
_UNPROBED = object()
_OPEN_FILES_CACHE: Dict[str, object] = {"at": 0.0, "paths": _UNPROBED, "broken": False}


def _codex_open_rollouts() -> Optional[set]:
    """Codex 进程当前打开着的会话文件集合；探不到返回 None。

    结果为 None 只表示"问不出来"（没有 lsof / 不是 unix / 命令失败），
    不表示"没有文件被打开"——调用方必须据此退回保守判定。
    熔断语义：一旦超时（系统级 lsof 卡死），本次运行内不再重试。
    """
    now = time.time()
    if _OPEN_FILES_CACHE.get("broken"):
        return None
    cached = _OPEN_FILES_CACHE.get("paths")
    if cached is not _UNPROBED and now - float(_OPEN_FILES_CACHE["at"] or 0.0) \
            < OPEN_FILES_CACHE_SECONDS:
        return cached  # type: ignore[return-value]
    found: Optional[set] = None
    try:
        completed = subprocess.run(["lsof", "-c", "codex", "-Fn"],
                                   capture_output=True, timeout=LSOF_TIMEOUT_SECONDS)
        # lsof 的退出码 0 = 有命中、1 = 没命中，两个都说明它本身跑成功了
        if completed.returncode in (0, 1):
            found = set()
            for raw in completed.stdout.decode("utf-8", "replace").splitlines():
                if raw.startswith("n") and raw.endswith(".jsonl"):
                    found.add(raw[1:])
    except (OSError, ValueError, subprocess.SubprocessError):
        # TimeoutExpired 也落在这里：探针超时 = 系统级 lsof 不可用，熔断
        found = None
        _OPEN_FILES_CACHE["broken"] = True
    _OPEN_FILES_CACHE["at"] = now
    _OPEN_FILES_CACHE["paths"] = found
    return found


def busy_reason(path: Path) -> Optional[str]:
    """这个会话文件现在能不能安全改写；None 表示可以。

    两个"不能碰"的信号取并集（保守优先）：
      ``codex-open``  Codex 正持有它的句柄 —— 换了 inode 会吃掉它后续的写入；
      ``recent``      最近 ``ACTIVE_GUARD_SECONDS`` 内被写过，句柄可能还热着。
    """
    opened = _codex_open_rollouts()
    if opened:
        try:
            if str(path) in opened:
                return "codex-open"
        except TypeError:  # pragma: no cover - 只在缓存被外部改坏时发生
            pass
    try:
        if time.time() - path.stat().st_mtime < ACTIVE_GUARD_SECONDS:
            return "recent"
    except OSError:
        return None
    return None

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
                        "code_interpreter_call", "image_generation_call",
                        # Codex 自己的内置工具。工具清单太长（插件 / 应用 / MCP）时，
                        # Codex 不再逐个列工具，而是发一个 tool_search 让模型自己搜。
                        # 它的 arguments 是**对象**，不像 function_call 那样是字符串
                        # —— 第三方平台两个都不认：工具类型直接拒（tools.N: tool type
                        # "tool_search" is not supported），解析 arguments 也会报
                        # "cannot unmarshal object into ... arguments of type string"。
                        # 严格校验请求体的平台（实测 Kimi / Moonshot）必须把它剥掉。
                        "tool_search_call", "tool_search_output"}


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
    """看一个 rollout 文件里有没有问题条目。只读，不改。

    逐行流式读，不 read_text：全量清扫会遍历用户**所有**会话文件，
    里面有几份几百 MB 的，一次性读进内存会把峰值顶到 GB 级。
    """
    result = {"path": str(path), "orphan_outputs": 0, "openai_only": 0,
              "cross_provider": 0, "image_outputs": 0,
              "report_only": {}, "lines": 0, "error": None}
    if not path.exists():
        result["error"] = "文件不存在"
        return result
    lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                lines += 1
                if "response_item" not in line:
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
                elif kind in ("function_call_output", "custom_tool_call_output") \
                        and payload.get("call_id") and _is_image_only_output(payload):
                    result["image_outputs"] += 1
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    result["lines"] = lines
    return result


def is_dirty(info: Dict, cross_provider: bool = False,
             moving_off_openai: bool = False) -> bool:
    """有没有需要清理的东西。

    cross_provider=False 时只看「确定是坏的」两类；
    要搬去别的平台时，OpenAI 专有条目和纯图片结果也得算进去。
    """
    dirty = bool(info.get("orphan_outputs") or info.get("openai_only"))
    if cross_provider:
        dirty = dirty or bool(info.get("cross_provider"))
    if (cross_provider or moving_off_openai) and info.get("image_outputs"):
        dirty = True
    return dirty


def _backup_root() -> Path:
    return paths.state_dir() / BACKUP_DIRNAME


# 纯图片工具结果的占位文本。保留 call_id，调用/结果配对完整；文本形态
# 是 Responses API 对 function_call_output 的标准形态，任何平台都认。
IMAGE_STUB_TEXT = ("[image elided] This tool output was an image that cannot be "
                   "replayed on a text-only model. The original view_image call "
                   "succeeded at the time; re-run the tool if the image is needed.")


def _is_image_only_output(payload: Dict) -> bool:
    """工具结果是不是只剩图片内容（文本一个字都没有）。"""
    out = payload.get("output")
    if not isinstance(out, list) or not out:
        return False
    items = [item for item in out if isinstance(item, dict)]
    if not items or len(items) != len(out):
        return False
    return all(item.get("type") == "input_image" for item in items)


def _image_stub_line(payload: Dict) -> str:
    """把纯图片结果改写成文本占位行。保留其余字段（尤其 call_id）。"""
    stub = dict(payload)
    stub["output"] = IMAGE_STUB_TEXT
    stub.pop("image_url", None)
    return json.dumps({"type": "response_item", "payload": stub},
                      ensure_ascii=False)


def prune_history_backups(root: Optional[Path] = None,
                          max_bytes: Optional[int] = None,
                          max_age_days: Optional[float] = None) -> Dict:
    """把备份目录压回上限内。按「先过期、再超量（最老的先删）」清理。

    只删备份副本，绝不碰 sessions/ 里的原始会话文件。每个文件都可能被
    多轮清扫反复备份（几百 MB 一份），不清就是无底洞。
    """
    root = root if root is not None else _backup_root()
    max_bytes = BACKUP_MAX_BYTES if max_bytes is None else max_bytes
    max_age_days = BACKUP_MAX_AGE_DAYS if max_age_days is None else max_age_days
    report = {"files": 0, "bytes_before": 0, "expired": 0, "over_limit": 0,
              "deleted": 0, "bytes_freed": 0, "bytes_after": 0}
    if not root.exists() or max_bytes <= 0:
        return report
    entries = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        report["files"] += 1
        report["bytes_before"] += stat.st_size
        entries.append((path, stat.st_mtime, stat.st_size))
    if not entries:
        report["bytes_after"] = report["bytes_before"]
        return report

    now = time.time()
    # 1) 过期的先删
    survivors = []
    for path, mtime, size in entries:
        if max_age_days > 0 and (now - mtime) > max_age_days * 86400:
            report["expired"] += _remove(path, report)
            continue
        survivors.append((path, mtime, size))

    # 2) 还超量就从最老的开始删，删到达标为止。
    #    这里必须是硬约束：如果"至少留一份"能架空上限，一份超大的备份
    #    就永远删不掉 —— 用户看到的还是无底洞。原始会话文件本体始终在
    #    sessions/ 里，备份删光了也只是放弃还原能力，不是丢数据。
    survivors.sort(key=lambda item: item[1])
    total = sum(size for _, _, size in survivors)
    for path, _mtime, size in survivors:
        if total <= max_bytes:
            break
        report["over_limit"] += _remove(path, report)
        total -= size
    report["bytes_after"] = report["bytes_before"] - report["bytes_freed"]
    return report


def _remove(path: Path, report: Dict) -> int:
    try:
        size = path.stat().st_size
        path.unlink()
        report["deleted"] += 1
        report["bytes_freed"] += size
        return 1
    except OSError:
        return 0


def _reuse_identical_backup(backup_dir: Path, original_name: str, text: str) -> Optional[Path]:
    """同一份文件内容已经备份过就别再复制一份。

    一个会话文件会被多轮清扫碰：第一轮清掉孤儿条目、第二轮清掉 OpenAI
    推理条目……每轮都整份复制的话，同一个文件能在备份目录里躺出好几个
    GB。内容没变就复用旧备份，变了才存新的。
    """
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    best = None
    try:
        for candidate in backup_dir.glob("*-" + original_name):
            if candidate.is_file() and (best is None
                                        or candidate.stat().st_mtime > best.stat().st_mtime):
                best = candidate
    except OSError:
        return None
    if best is None:
        return None
    try:
        if best.stat().st_size != len(text.encode("utf-8", "replace")):
            return None
        if hashlib.sha256(best.read_bytes()).hexdigest() == digest:
            return best
    except OSError:
        return None
    return None


def sanitize(path: Path, moving_off_openai: bool, backup_dir: Path,
             cross_provider: bool = False) -> Tuple[bool, Dict]:
    """删掉指定文件里的问题条目。返回 (是否改动, 统计)。

    cross_provider=True 时额外剥离 OpenAI 服务端工具的成对条目，
    这样这个会话才能搬到第三方平台上继续。
    """
    stats = {"removed_orphan_outputs": 0, "removed_openai_only": 0,
             "removed_cross_provider": 0, "removed_image_outputs": 0,
             "kept": 0, "backup": None}
    deep_clean = moving_off_openai or cross_provider
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, stats

    lines = text.split("\n")
    kept: List[str] = []
    for line in lines:
        payload = _payload_of(line) if cross_provider else (
            _payload_of(line) if ('function_call_output' in line
                                  or 'encrypted_content' in line
                                  or deep_clean and '"input_image"' in line) else None)
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
            # 纯图片的工具结果：不支持视觉的模型上，Codex 构建请求时会把
            # 图片结果剥掉、调用却留着 —— API 校验「有调用没结果」直接 400
            # （报 No tool output found for tool call ...）。实测一个文件里
            # 埋了 50 个、每个约 2MB。换成文本占位并保留 call_id，
            # 调用/结果配对完整，任何平台都能收。
            if deep_clean and kind in ("function_call_output",
                                       "custom_tool_call_output") \
                    and payload.get("call_id") and _is_image_only_output(payload):
                stats["removed_image_outputs"] += 1
                kept.append(_image_stub_line(payload))
                continue
        if line:
            kept.append(line)
    stats["kept"] = len(kept)

    if not (stats["removed_orphan_outputs"] or stats["removed_openai_only"]
            or stats["removed_cross_provider"] or stats["removed_image_outputs"]):
        return False, stats

    # 备份：保留原文件，文件名带时间戳，放工具自己的状态目录里。
    # 内容和最近一次备份完全一样就复用，不再整份复制 —— 一个几百 MB 的
    # 会话被多轮清扫碰上，照旧整份复制的话备份目录几天就能吃掉上百 GB。
    try:
        relative = path.relative_to(paths.codex_home())
    except ValueError:
        relative = Path(path.name)
    reused = _reuse_identical_backup(backup_dir, relative.name, text)
    if reused is not None:
        stats["backup"] = str(reused)
    else:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
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
              "removed": {"orphan_outputs": 0, "openai_only": 0, "cross_provider": 0,
                          "image_outputs": 0}}
    for path in paths_to_clean:
        info = inspect(path)
        if not is_dirty(info, cross_provider=cross_provider,
                        moving_off_openai=moving_off_openai):
            continue
        if busy_reason(path):
            report["skipped_active"].append(str(path))
            continue
        if dry_run:
            report["items"].append({"path": str(path), "would_remove": {
                "orphan_outputs": info["orphan_outputs"],
                "openai_only": info["openai_only"] if moving_off_openai else 0,
                "cross_provider": info["cross_provider"] if cross_provider else 0,
                "image_outputs": info["image_outputs"]
                if (moving_off_openai or cross_provider) else 0}})
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
            report["removed"]["image_outputs"] += stats["removed_image_outputs"]
            report["items"].append({"path": str(path), "removed": {
                "orphan_outputs": stats["removed_orphan_outputs"],
                "openai_only": stats["removed_openai_only"],
                "cross_provider": stats["removed_cross_provider"],
                "image_outputs": stats["removed_image_outputs"]}})
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
        if busy_reason(path):
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


# ------------------------------------------------------------ 全量清扫（不走窗口）

# 为什么还要有这一层
# ------------------
# 上面 auto_clean 只扫"最近 30 个"，repair 只扫"最近 10 个"——它们回答的是
# 「刚切换完，用户马上要接着用的那几个对话干不干净」。但坏条目不挑新旧：
#
#   实测（2026-09-17）：用户机器上 1347 个会话文件、33.9GB，其中 **45 个文件
#   里有 72 条** `function_call_output` 缺 `call_id`，**全部**来自 `codex_app`
#   命名空间的工具（automation_update 等）。最老的一条躺在 7 月底的小文件里。
#
# 病根是 Codex 两套类型定义的不对称：
#   · 会话文件里（ResponseItem）  call_id: Option<String> + skip_if_none —— 可缺省
#   · 发给 API 时（ResponseInputItem） call_id: String —— 必填
# 于是 Codex App 自带工具写下的"只有输出、没有调用"的记录能存进文件，
# 一旦被回放（继续对话，连后台"生成线程描述"的结构化回合也算）就必炸：
#     Failed to deserialize the JSON body into the target type:
#     input: missing field `call_id` at line 1 column N
#
# 只扫最近的窗口永远扫不到三个月前那个文件，可用户哪天点开它就炸一次。
# 所以这里**不设窗口**：全部过一遍，坏的就清掉。
#
# 增量靠账本：path → [mtime_ms, size]。没变过的文件只做一次 stat；
# 第一次全量之后每轮几乎零成本。busy 的文件**不记账**，下一轮接着来。

SWEEP_LEDGER_NAME = "history-sweep.json"
SWEEP_BUDGET_SECONDS = 25.0


def _sweep_ledger_path() -> Path:
    return paths.state_dir() / SWEEP_LEDGER_NAME


def _load_sweep_ledger() -> Dict:
    try:
        data = json.loads(_sweep_ledger_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_sweep_ledger(ledger: Dict) -> None:
    try:
        paths.ensure_dir(paths.state_dir())
        _sweep_ledger_path().write_text(json.dumps(ledger), encoding="utf-8")
    except OSError:
        pass  # 写不进去只是下一轮多扫一遍，不影响功能


def _fingerprint(path: Path):
    """文件的"变过没有"指纹；读不到返回 None。"""
    try:
        info = path.stat()
    except OSError:
        return None
    return [int(info.st_mtime * 1000), int(info.st_size)]


def _sweep_mode(cross_provider: bool, moving_off_openai: bool) -> str:
    """账本要按"清洗力度"分桶。

    否则先跑一次默认（只清孤儿）把文件记成"干净"，再跑 --cross-provider
    就会被账本跳过，深度模式等于白跑。
    """
    return "%s%s" % ("c" if cross_provider else "-", "o" if moving_off_openai else "-")


def _would_change(info: Dict, cross_provider: bool, moving_off_openai: bool) -> bool:
    """这份文件在这个清洗力度下**真的**会被改动吗。

    必须和 :func:`sanitize` 的删除条件逐条对齐，不能沿用 ``is_dirty``：
    ``is_dirty`` 只要看到 ``openai_only`` 就算脏，但 ``moving_off_openai=False``
    时 sanitize 根本不会删它。实测本机 1290 个文件带 ``encrypted_content``
    推理（11.6 万条），而且用户正在 deepseek 上用得好好的 —— 它是被容忍的，
    不是坏数据。沿用 is_dirty 会让干跑报告虚高 1290 条，还白白多读几十 GB。
    """
    if info.get("orphan_outputs"):
        return True
    if moving_off_openai and info.get("openai_only"):
        return True
    if cross_provider and info.get("cross_provider"):
        return True
    if (cross_provider or moving_off_openai) and info.get("image_outputs"):
        return True
    return False


def sweep_all(moving_off_openai: bool = False, cross_provider: bool = False,
              limit: Optional[int] = None, budget_seconds: float = SWEEP_BUDGET_SECONDS,
              dry_run: bool = False, force: bool = False) -> Dict:
    """把**全部**会话文件过一遍，清掉会让平台拒收请求的条目。

    cross_provider=False（默认）只清"任何平台都不认"的孤儿输出——它在官方和
    第三方都是坏数据，可以无条件清。要顺带剥离别家服务端工具条目（web_search
    等），传 cross_provider=True，那属于「这个会话要整体搬到第三方」的动作。

    从最新修改的开始处理：最新的最可能被 resume。超预算就收工，
    剩下的下一轮接着扫（账本保证不会重复读没变过的文件）。
    """
    started = time.time()
    report = {"scanned": 0, "checked": 0, "cleaned": 0, "skipped_busy": 0,
              "skipped_cached": 0, "budget_exhausted": False, "dry_run": dry_run,
              "cross_provider": cross_provider, "backup_dir": None, "items": [],
              "removed": {"orphan_outputs": 0, "openai_only": 0, "cross_provider": 0,
                          "image_outputs": 0}}
    try:
        rollouts = recent_rollouts(0 if limit is None else max(0, int(limit)))
    except OSError:
        return report

    ledger = {} if force else _load_sweep_ledger()
    next_ledger = dict(ledger)
    mode = _sweep_mode(cross_provider, moving_off_openai)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _backup_root() / stamp

    for path in rollouts:
        if time.time() - started > budget_seconds:
            report["budget_exhausted"] = True
            break
        report["scanned"] += 1
        key = mode + "|" + str(path)
        fingerprint = _fingerprint(path)
        if fingerprint is None:
            continue
        if ledger.get(key) == fingerprint:
            report["skipped_cached"] += 1
            continue
        if busy_reason(path):
            report["skipped_busy"] += 1
            continue  # 不记账：等它静下来下一轮再来
        report["checked"] += 1
        info = inspect(path)
        if not _would_change(info, cross_provider, moving_off_openai):
            next_ledger[key] = fingerprint
            continue
        if dry_run:
            report["cleaned"] += 1
            report["items"].append({"path": str(path), "would_remove": {
                "orphan_outputs": info["orphan_outputs"],
                "openai_only": info["openai_only"] if moving_off_openai else 0,
                "cross_provider": info["cross_provider"] if cross_provider else 0,
                "image_outputs": info["image_outputs"]
                if (moving_off_openai or cross_provider) else 0}})
            continue
        changed, stats = sanitize(path, moving_off_openai, backup_dir,
                                  cross_provider=cross_provider)
        if changed:
            report["cleaned"] += 1
            report["backup_dir"] = str(backup_dir)
            for field in report["removed"]:
                report["removed"][field] += stats.get("removed_" + field, 0)
            report["items"].append({"path": str(path), "removed": {
                "orphan_outputs": stats["removed_orphan_outputs"],
                "openai_only": stats["removed_openai_only"],
                "cross_provider": stats["removed_cross_provider"]}})
            after = _fingerprint(path)
            next_ledger[key] = after if after is not None else fingerprint
        else:
            next_ledger[key] = fingerprint

    if not dry_run:
        _save_sweep_ledger(next_ledger)
    # 清完把备份目录压回上限内。备份必须在每轮清扫里自我约束，否则
    # 会话文件几百 MB 一份、清扫又高频发生，几天就能吃掉上百 GB。
    if not dry_run:
        try:
            report["backup_prune"] = prune_history_backups()
        except Exception:  # noqa: BLE001 - 清理失败不能影响清扫结果本身
            pass
    return report


def describe_sweep(report: Dict) -> str:
    """把全量清扫的结果说成一句人话。"""
    if report.get("dry_run"):
        head = "预演：扫了 %d 个会话文件，%d 个需要清理" % (
            report["scanned"], report["cleaned"])
    else:
        head = "清扫完毕：扫了 %d 个会话文件，清理了 %d 个" % (
            report["scanned"], report["cleaned"])
    lines = [head]
    removed = report.get("removed") or {}
    if removed.get("orphan_outputs"):
        lines.append("  · 删掉缺 call_id 的工具结果 %d 条（就是它导致 missing field `call_id`）"
                     % removed["orphan_outputs"])
    if removed.get("openai_only"):
        lines.append("  · 删掉 OpenAI 专有推理条目 %d 条" % removed["openai_only"])
    if removed.get("cross_provider"):
        lines.append("  · 成对剥离别家服务端工具条目 %d 条" % removed["cross_provider"])
    if report.get("skipped_busy"):
        lines.append("  · %d 个会话 Codex 正开着，等它静下来会自动补上"
                     % report["skipped_busy"])
    if report.get("budget_exhausted"):
        lines.append("  · 时间预算用完，剩下的下一轮继续")
    if report.get("backup_dir"):
        lines.append("  备份：%s" % report["backup_dir"])
    if not report.get("cleaned") and not report.get("skipped_busy"):
        lines.append("  没有发现需要清理的内容 ✅")
    return "\n".join(lines)
