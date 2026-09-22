"""任务（对话）与服务商的绑定修复。

为什么需要它
------------
Codex 把「这个任务属于哪个服务商」写死在三处：

  1. ``state_5.sqlite`` 的 ``threads.model_provider``
  2. ``sqlite/codex-dev.db`` 的 ``local_thread_catalog.model_provider``
  3. 会话文件里的 ``session_meta.payload.model_provider``
     以及每一轮的 ``event_msg.payload.thread_settings.model_provider_id``

切换默认服务商**不会**回头改旧任务。于是在一个绑定 OpenAI 的旧任务里选
DeepSeek 的模型，请求会带着 DeepSeek 的模型名发到 ChatGPT 账号，服务端回：

    The 'deepseek-flash' model is not supported when using Codex with a ChatGPT account.

修复原则很简单：**任务用的模型属于哪个平台，任务的服务商就应该是哪个平台。**
只有这一种情况会被改写，其它一律不碰；改前全部备份。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths, registry

STATE_DB = "state_5.sqlite"
CATALOG_DB = "sqlite/codex-dev.db"
BACKUP_DIRNAME = "thread-backups"
# 深度扫描会跳过最近还在写入的会话文件：Codex 可能正拿它追加内容，
# 这时替换文件会让它后续写入落到已经被替换掉的旧 inode 上。
ACTIVE_GUARD_SECONDS = 120
# 「数据库对了、会话文件里还留着旧平台」的扫描范围。会话文件动辄几百 MB，
# 全量扫一遍要好几分钟，所以默认只扫最近这几个还在动的任务，--deep 才全量。
FILE_SCAN_RECENT = 10
FILE_SCAN_BUDGET_SECONDS = 5.0
# 切换平台时，最近这段时间里还在动的任务跟着一起搬过去。
FOLLOW_WINDOW_SECONDS = 36 * 3600

# 模型名可能带这些前缀，匹配时先剥掉
PREFIXES = ("codex-",)

# 官方（ChatGPT 账号）服务商的代号。这里不 import engine，免得循环导入。
OFFICIAL_PROVIDER_ID = "openai"


class ThreadError(RuntimeError):
    pass


# ------------------------------------------------------------------ 模型归属

def normalize_model(model: str) -> str:
    text = (model or "").strip()
    for prefix in PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    return text


def owner_map(state: Dict) -> Dict[str, str]:
    """模型名 → 拥有它的服务商。

    来源：各个平台的模型目录 + 内置预设的已知模型。
    """
    owners: Dict[str, str] = {}
    for provider_id, record in (state.get("providers") or {}).items():
        if provider_id == "openai":
            continue
        for model_id in (record.get("models") or {}).keys():
            owners.setdefault(model_id.lower(), provider_id)
            owners.setdefault(normalize_model(model_id).lower(), provider_id)
    for preset in registry.PRESETS:
        for model_id in preset.get("known_models") or []:
            owners.setdefault(model_id.lower(), preset["id"])
    return owners


def expected_provider(model: str, owners: Dict[str, str]) -> Optional[str]:
    """这个模型应该由哪个服务商提供；不认识就返回 None（不动它）。"""
    if not model:
        return None
    key = normalize_model(model).lower()
    return owners.get(key) or owners.get(model.lower())


# -------------------------------------------------------------------- 读取

def _connect(path: Path, readonly: bool = True) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        if readonly:
            return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        return sqlite3.connect(str(path), timeout=10)
    except sqlite3.Error:
        return None


def list_threads(limit: Optional[int] = None) -> List[Dict]:
    """从 state_5.sqlite 读任务，附带会话文件路径。

    source 字段标记任务来源：``exec`` 是定时/自动化任务（将来会自动开跑），
    切换平台时必须无条件跟着搬，所以这里尽量把它读出来；老库里没有这个
    列就退回不带 source 的查询。
    """
    db = paths.codex_home() / STATE_DB
    connection = _connect(db)
    if connection is None:
        return []
    base = ("SELECT id, title, model, model_provider, rollout_path, updated_at%s "
            "FROM threads ORDER BY updated_at DESC")
    try:
        sql = base % ", source"
        if limit:
            sql += " LIMIT %d" % int(limit)
        try:
            rows = connection.execute(sql).fetchall()
            has_source = True
        except sqlite3.Error:
            sql = base % ""
            if limit:
                sql += " LIMIT %d" % int(limit)
            rows = connection.execute(sql).fetchall()
            has_source = False
    except sqlite3.Error:
        connection.close()
        return []
    finally:
        connection.close()
    return [
        {"id": row[0], "title": row[1] or "", "model": row[2] or "",
         "provider": row[3] or "", "rollout_path": row[4] or "", "updated_at": row[5],
         "source": (row[6] or "") if has_source else ""}
        for row in rows
    ]


def find_mismatches(state: Optional[Dict] = None, limit: Optional[int] = None) -> List[Dict]:
    """找出「模型属于 A 平台，却被记成 B 平台」的任务。"""
    from . import state as state_module
    state = state or state_module.load()
    owners = owner_map(state)
    bad = []
    for item in list_threads(limit=limit):
        want = expected_provider(item["model"], owners)
        if want and want != item["provider"]:
            bad.append(dict(item, expected=want))
    return bad


def summarize() -> Dict:
    """给界面用：总数 / 不匹配数 / 明细。"""
    threads = list_threads()
    from . import state as state_module
    mismatch = find_mismatches()
    return {
        "total": len(threads),
        "mismatch": len(mismatch),
        "items": mismatch[:50],
    }


# -------------------------------------------------------------------- 修复

def _backup_root() -> Path:
    return paths.ensure_dir(paths.state_dir() / BACKUP_DIRNAME)


def _line_may_hold_provider(line: str) -> bool:
    """粗筛：这一行可能记录着服务商。

    注意要用不带引号的 ``model_provider`` —— ``"model_provider_id"`` 里并不
    包含 ``"model_provider"``（带右引号），早期版本在这里漏掉了全部轮次设置。
    """
    return "model_provider" in line


def _rewrite_session_file(path: Path, from_provider: Optional[str], to_provider: str,
                          backup_dir: Path, *, host_closed: bool = False) -> Tuple[bool, int, int, int]:
    """Explicit provider migration. Keep all source history records intact.

    Only provider metadata and supported official item IDs may change. Never
    remove messages, tools, images or reasoning to satisfy a remote API.
    """
    from . import history as history_module
    from .history_write_guard import prepare_rewrite

    if not path.exists():
        return False, 0, 0, 0

    def should_replace(value) -> bool:
        if value is None:
            return False
        if from_provider is None:
            return value != to_provider
        return value == from_provider

    if history_module.busy_reason(path):
        raise OSError("Session safety cannot be verified; migration deferred")
    original_stat = path.stat()
    original_bytes = path.read_bytes()
    raw_lines: List[bytes] = []
    meta_changed = 0
    settings_changed = 0
    history_removed = 0
    changed = False
    # Work from the verified byte snapshot so untouched lines, blank lines and
    # each original line ending survive the migration exactly.
    for raw_line in original_bytes.splitlines(keepends=True):
        line = raw_line.decode("utf-8", "surrogateescape")
        is_response = '"response_item"' in line or '"compacted"' in line
        if not _line_may_hold_provider(line) and not is_response:
            raw_lines.append(raw_line)
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(raw_line)
            continue
        if not isinstance(document, dict) or not isinstance(document.get("payload"), dict):
            raw_lines.append(raw_line)
            continue
        payload = document["payload"]
        # Preserve every historical record. Protocol adaptation must never
        # delete source records or their tool results from the transcript.
        touched = False
        if to_provider == OFFICIAL_PROVIDER_ID:
            from .message_ids import normalize_record
            touched = bool(normalize_record(document))
        if document.get("type") == "session_meta" and should_replace(payload.get("model_provider")):
            payload["model_provider"] = to_provider
            meta_changed += 1
            touched = True
        settings = payload.get("thread_settings")
        if isinstance(settings, dict) and should_replace(settings.get("model_provider_id")):
            settings["model_provider_id"] = to_provider
            settings_changed += 1
            touched = True
        if touched:
            changed = True
            ending = b"\r\n" if raw_line.endswith(b"\r\n") else b"\n" if raw_line.endswith(b"\n") else b""
            raw_lines.append(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8", "surrogateescape") + ending)
        else:
            raw_lines.append(raw_line)
    if not (changed or history_removed):
        return False, 0, 0, 0

    if (history_module.busy_reason(path)
            or not history_module.same_snapshot(path, original_stat, original_bytes)):
        raise OSError("Session changed during migration; deferred")
    # 备份原文件（保留目录结构，方便对照）
    relative = path.name
    target = backup_dir / (str(time.time_ns()) + "-" + relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        os.chmod(target, 0o600)
        stream.write(original_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    if target.read_bytes() != original_bytes:
        raise OSError("Session backup readback mismatch; original left unchanged")

    updated = b"".join(raw_lines)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, path.stat().st_mode & 0o777)
        if history_module.busy_reason(path) or not history_module.same_snapshot(path, original_stat, original_bytes):
            raise OSError("Session changed during migration; deferred")
        prepare_rewrite(path, host_closed=host_closed)
        if history_module.busy_reason(path) or not history_module.same_snapshot(path, original_stat, original_bytes):
            raise OSError("Session changed during migration; deferred")
        os.replace(temp, path)
        if path.read_bytes() != updated:
            raise OSError("Session readback mismatch; original retained in backup")
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True, meta_changed, settings_changed, history_removed


def file_has_stale_provider(path: Path, expected: str) -> bool:
    """会话文件里是否还留着别的服务商（会话头或任意一轮的设置）。"""
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                if not _line_may_hold_provider(line):
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = document.get("payload")
                if not isinstance(payload, dict):
                    continue
                if document.get("type") == "session_meta":
                    value = payload.get("model_provider")
                    if value and value != expected:
                        return True
                settings = payload.get("thread_settings")
                if isinstance(settings, dict):
                    value = settings.get("model_provider_id")
                    if value and value != expected:
                        return True
    except OSError:
        return False
    return False


def _update_databases(thread_id: str, from_provider: str, to_provider: str,
                      backup_dir: Path, model: Optional[str] = None) -> List[str]:
    """同步两个 sqlite。返回被改动的库名。

    model 给了就顺手把模型名一起改掉：任务要整个搬到新平台上，
    只换服务商不换模型，下次请求还是拿着旧模型名去敲新平台的门。
    """
    touched = []
    for label, relative, table, column in (
        ("state_5", STATE_DB, "threads", "model_provider"),
        ("catalog", CATALOG_DB, "local_thread_catalog", "model_provider"),
    ):
        db = paths.codex_home() / relative
        connection = _connect(db, readonly=False)
        if connection is None:
            continue
        try:
            # 备份一次就够了
            snapshot = backup_dir / (db.name + ".before")
            if not snapshot.exists():
                try:
                    # 用 sqlite 自己的备份接口，带上 WAL 里还没落盘的内容
                    with sqlite3.connect(str(db), timeout=5) as source, \
                            sqlite3.connect(str(snapshot)) as destination:
                        source.backup(destination)
                except sqlite3.Error:
                    shutil.copy2(db, snapshot)
            key = "id" if table == "threads" else "thread_id"
            # 目录表（local_thread_catalog）没有 model 列，只有服务商
            if model and table == "threads":
                cursor = connection.execute(
                    "UPDATE %s SET %s = ?, model = ? WHERE %s = ? AND %s = ?"
                    % (table, column, key, column),
                    (to_provider, model, thread_id, from_provider))
            else:
                cursor = connection.execute(
                    "UPDATE %s SET %s = ? WHERE %s = ? AND %s = ?" % (table, column, key, column),
                    (to_provider, thread_id, from_provider))
            connection.commit()
            if cursor.rowcount:
                touched.append(label)
        except sqlite3.Error as error:
            raise ThreadError("更新 %s 失败：%s" % (label, error))
        finally:
            connection.close()
    return touched


def repair(thread_id: Optional[str] = None, dry_run: bool = False,
           limit: Optional[int] = None, deep: bool = False) -> Dict:
    """把不匹配的任务对齐。thread_id 为空时修复全部。

    deep=True 时还会清理「数据库已经对了、但会话文件里还留着旧服务商」的情况
    （需要逐个读会话文件，慢一些，所以默认关闭）。
    """
    from . import state as state_module
    state = state_module.load()
    owners = owner_map(state)
    all_threads = list_threads(limit=limit)
    candidates = []
    for item in all_threads:
        want = expected_provider(item["model"], owners)
        if not want or want == item["provider"]:
            continue
        if thread_id and not item["id"].startswith(thread_id):
            continue
        candidates.append(dict(item, expected=want))

    stale_files: List[Dict] = []
    skipped_active: List[Dict] = []
    # 会话文件扫描：数据库对得上、文件里却还留着旧服务商的情况，以前只有 --deep
    # 才管，结果用户切完平台继续任务照样报错。现在默认也扫，但只扫最近几个还在
    # 动的任务，并且卡时间预算——一个会话文件几百 MB，不能让界面卡在这上面。
    scan_targets = all_threads if deep else all_threads[:max(0, FILE_SCAN_RECENT)]
    scan_started = time.time()
    for item in scan_targets:
        if time.time() - scan_started > FILE_SCAN_BUDGET_SECONDS:
            break
        if thread_id and not item["id"].startswith(thread_id):
            continue
        # 数据库里服务商是空的就别猜了：拿空串去对齐会把会话文件里的值抹掉
        if not item["provider"]:
            continue
        path = Path(item["rollout_path"]) if item["rollout_path"] else None
        if not path or not path.exists():
            continue
        if item["id"] in {c["id"] for c in candidates}:
            continue  # 上面那轮已经会整份重写
        try:
            if time.time() - path.stat().st_mtime < ACTIVE_GUARD_SECONDS:
                skipped_active.append(item)
                continue
        except OSError:
            continue
        if file_has_stale_provider(path, item["provider"]):
            stale_files.append(item)

    report = {"checked": len(all_threads), "fixed": 0, "dry_run": dry_run,
              "items": [], "backup_dir": None, "deep": deep,
              "skipped_active": [{"id": item["id"][:8], "title": item["title"][:40]}
                                 for item in skipped_active]}
    if not candidates and not stale_files:
        return report
    if dry_run:
        report["items"] = [{"id": c["id"][:8], "title": c["title"][:40], "model": c["model"],
                            "from": c["provider"], "to": c["expected"]} for c in candidates]
        for item in stale_files:
            report["items"].append({"id": item["id"][:8], "title": item["title"][:40],
                                    "model": item["model"], "from": "会话文件里的旧值",
                                    "to": item["provider"]})
        return report

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _backup_root() / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    report["backup_dir"] = str(backup_dir)

    for item in candidates:
        entry = {"id": item["id"][:8], "title": item["title"][:40], "model": item["model"],
                 "from": item["provider"], "to": item["expected"],
                 "session": False, "meta": 0, "settings": 0, "db": []}
        path = Path(item["rollout_path"]) if item["rollout_path"] else None
        try:
            if not path or not path.exists():
                raise OSError("Session file unavailable")
            changed, meta, settings, cleaned = _rewrite_session_file(
                path, item["provider"], item["expected"], backup_dir)
            if file_has_stale_provider(path, item["expected"]):
                raise OSError("Session still has mismatched provider")
            entry.update(session=changed, meta=meta, settings=settings, history=cleaned)
        except (OSError, ValueError):
            entry["active"] = True
            report["skipped_active"].append({"id": entry["id"], "title": entry["title"]})
            report["items"].append(entry)
            continue
        entry["db"] = _update_databases(item["id"], item["provider"], item["expected"], backup_dir)
        report["items"].append(entry)
        if entry["session"] or entry["db"]:
            report["fixed"] += 1

    for item in stale_files:
        path = Path(item["rollout_path"])
        try:
            if time.time() - path.stat().st_mtime < ACTIVE_GUARD_SECONDS:
                report["skipped_active"].append(
                    {"id": item["id"][:8], "title": item["title"][:40]})
                continue
        except OSError:
            continue
        try:
            changed, meta, settings, cleaned = _rewrite_session_file(
                path, None, item["provider"], backup_dir)
        except (OSError, ValueError):
            report["skipped_active"].append({"id": item["id"][:8], "title": item["title"][:40]})
            continue
        if changed:
            report["items"].append({
                "id": item["id"][:8], "title": item["title"][:40], "model": item["model"],
                "from": "会话文件旧值", "to": item["provider"],
                "session": True, "meta": meta, "settings": settings, "db": [],
                "history": cleaned})
            report["fixed"] += 1
    return report


def follow_switch(from_provider: str, to_provider: str, model: Optional[str] = None,
                  window_seconds: Optional[float] = FOLLOW_WINDOW_SECONDS,
                  include_openai: bool = False, dry_run: bool = False,
                  limit: Optional[int] = None) -> Dict:
    """切换平台时，把「还在用」的旧任务一起搬过去。

    为什么非搬不可
    --------------
    Codex 恢复一个旧任务时，**服务商取任务自己记的那个**（会话文件里的
    ``session_meta.model_provider`` 和每轮的 ``thread_settings.model_provider_id``），
    **模型名却取当前配置里的那个**。两者一分家，请求就带着新平台的模型名
    敲进旧平台的接口，服务端直接回：

        invalid params, code: 2013, msg: invalid params, unknown model 'deepseek-flash'
        （把 deepseek 的模型名发给 MiniMax 时 MiniMax 的原话）

    也就是说：只改 ``config.toml`` 根本不算切换完，旧任务的绑定也得跟着走，
    否则用户切完一继续任务就炸。所以这里把最近还在动的任务整条搬过去：
    会话文件里的服务商 + 两个数据库里的服务商与模型名，改前全部备份。

    挑选规则（2026-09-17 起，保证「切换后所有任务都能正常跑」）：
      · **定时/自动化任务（source = exec）无条件跟随** —— 它们不靠人点开，
        到点自动开跑，绑定不对就是静默炸掉；不管多老、不管从哪家搬，必搬；
      · 其余任务：window_seconds 窗口内的跟着搬（默认 36h）；
      · window_seconds=None + include_openai=True：全部搬 —— 这是后台全量
        迁移用的组合，把 ChatGPT 的老任务也搬干净；
      · limit：一次最多搬多少条（后台分批迁移用，避免一次改动太多文件）。
    """
    empty = {"checked": 0, "moved": 0, "items": [], "skipped_active": [],
             "dry_run": dry_run, "backup_dir": None}
    if not from_provider or not to_provider or from_provider == to_provider:
        return empty

    threads = list_threads()
    cutoff = None if window_seconds is None else time.time() - max(0.0, window_seconds)

    def eligible(item: Dict) -> bool:
        if item["provider"] != from_provider:
            return False
        is_exec = (item.get("source") or "").strip() == "exec"
        if from_provider == OFFICIAL_PROVIDER_ID and not include_openai:
            # ChatGPT 的任务不默认搬：那是「老家」，一搬就是上千条。
            # 唯独自动化任务例外 —— 它们到点自己跑，必须保证能跑。
            return is_exec
        if is_exec:
            return True
        return True if cutoff is None else (item["updated_at"] or 0) >= cutoff

    candidates = [item for item in threads if eligible(item)]
    if limit is not None:
        candidates = candidates[:max(0, int(limit))]
    report = {"checked": len(threads), "moved": 0, "items": [], "skipped_active": [],
              "dry_run": dry_run, "backup_dir": None,
              "from": from_provider, "to": to_provider, "model": model,
              "exec_followed": 0}
    if dry_run:
        report["items"] = [{"id": item["id"][:8], "title": item["title"][:40],
                            "model": item["model"], "from": from_provider, "to": to_provider}
                           for item in candidates]
        return report

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = _backup_root() / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    report["backup_dir"] = str(backup_dir)

    for item in candidates:
        entry = {"id": item["id"][:8], "title": item["title"][:40], "model": item["model"],
                 "from": from_provider, "to": to_provider,
                 "session": False, "meta": 0, "settings": 0, "db": [], "active": False}
        path = Path(item["rollout_path"]) if item["rollout_path"] else None
        try:
            if not path or not path.exists():
                raise OSError("Session file unavailable")
            changed, meta, settings, cleaned = _rewrite_session_file(
                path, from_provider, to_provider, backup_dir)
            if file_has_stale_provider(path, to_provider):
                raise OSError("Session still has mismatched provider")
            entry.update(session=changed, meta=meta, settings=settings, history=cleaned)
        except (OSError, ValueError):
            entry["active"] = True
            report["skipped_active"].append(entry["id"])
            report["items"].append(entry)
            continue
        entry["db"] = _update_databases(
            item["id"], from_provider, to_provider, backup_dir, model=model)
        if (item.get("source") or "").strip() == "exec":
            report["exec_followed"] += 1
        report["items"].append(entry)
        if entry["session"] or entry["db"]:
            report["moved"] += 1
    return report


def describe_follow(report: Dict) -> str:
    """把 follow_switch 的结果说清楚。"""
    if not report.get("items"):
        return "没有需要跟着搬的任务。"
    if report.get("dry_run"):
        lines = ["预演：将把 %d 个任务从 %s 搬到 %s"
                 % (len(report["items"]), report["from"], report["to"])]
    else:
        lines = ["已把 %d 个任务从 %s 搬到 %s" % (report["moved"], report["from"], report["to"])]
    for item in report["items"][:12]:
        extra = []
        if item.get("session"):
            extra.append("会话 %d 处头 / %d 处轮次" % (item["meta"], item["settings"]))
        if item.get("history"):
            extra.append("剥离跨平台条目 %d 条" % item["history"])
        if item.get("db"):
            extra.append("数据库 " + "、".join(item["db"]))
        if item.get("active"):
            extra.append("尚未修改：历史文件未通过安全检查")
        lines.append("  %s  %s" % (item["id"], "；".join(extra) if extra else "无改动"))
    if report.get("backup_dir"):
        lines.append("备份：%s" % report["backup_dir"])
    return "\n".join(lines)


# ------------------------------------------------------------------ Safe task binding policy
#
# A Codex task is a persisted protocol transcript, not just a model selection.
# Rewriting its provider, model, message identifiers, or response records makes
# the host re-submit one provider's opaque state to another provider.  That was
# the source of the invalid `msg` / `fc` prefixes, malformed reasoning content,
# and disappearing-history regressions seen in older releases.  Keep the old
# low-level migration helpers above for forensic compatibility only; public
# operations below deliberately override them and are diagnostic-only.

_CONTINUATION_REQUIRED = (
    "任务已绑定原服务商。为保护完整历史，切换默认模型不会改写已有任务；"
    "请在目标服务商下新建或分叉兼容续接任务。"
)


def _binding_candidates(thread_id: Optional[str] = None,
                        limit: Optional[int] = None) -> List[Dict]:
    """Return bindings that cannot safely be repaired in place.

    The function intentionally does not inspect or write rollout files.  A
    database/model mismatch is useful advice for the UI, but never proof that
    it is safe to mutate an opaque conversation transcript.
    """
    from . import state as state_module
    owners = owner_map(state_module.load())
    candidates = []
    for item in list_threads(limit=limit):
        if thread_id and not item["id"].startswith(thread_id):
            continue
        expected = expected_provider(item["model"], owners)
        if expected and expected != item["provider"]:
            candidates.append(dict(item, expected=expected))
    return candidates


def repair(thread_id: Optional[str] = None, dry_run: bool = False,
           limit: Optional[int] = None, deep: bool = False) -> Dict:
    """Diagnose provider/model binding mismatches without changing history.

    ``deep`` is retained for CLI/API compatibility but never authorizes a
    write.  This makes startup checks, watchdog checks, and the App's repair
    button safe even when the desktop host is actively appending history.
    """
    all_threads = list_threads(limit=limit)
    candidates = _binding_candidates(thread_id=thread_id, limit=limit)
    items = [{
        "id": item["id"][:8], "title": item["title"][:40],
        "model": item["model"], "from": item["provider"],
        "to": item["expected"], "continuation_required": True,
    } for item in candidates]
    return {
        "checked": len(all_threads), "fixed": 0, "moved": 0,
        "dry_run": dry_run, "deep": deep, "items": items,
        "backup_dir": None, "skipped_active": [],
        "continuation_required": len(items), "message": _CONTINUATION_REQUIRED,
        "history_preserved": True,
    }


def follow_switch(from_provider: str, to_provider: str, model: Optional[str] = None,
                  window_seconds: Optional[float] = FOLLOW_WINDOW_SECONDS,
                  include_openai: bool = False, dry_run: bool = False,
                  limit: Optional[int] = None) -> Dict:
    """Plan compatible continuations instead of migrating existing tasks.

    ``window_seconds`` and ``include_openai`` remain accepted so old callers
    cannot accidentally fall back to the historic mutating implementation.
    """
    records = [item for item in list_threads(limit=limit)
               if item["provider"] == from_provider and from_provider != to_provider]
    if window_seconds is not None:
        cutoff = time.time() - max(0.0, window_seconds)
        records = [item for item in records
                   if (item.get("updated_at") or 0) >= cutoff]
    if limit is not None:
        records = records[:max(0, int(limit))]
    items = [{
        "id": item["id"][:8], "title": item["title"][:40],
        "model": item["model"], "from": from_provider, "to": to_provider,
        "continuation_required": True,
    } for item in records]
    return {
        "checked": len(list_threads()), "moved": 0, "fixed": 0,
        "items": items, "skipped_active": [], "dry_run": dry_run,
        "backup_dir": None, "from": from_provider, "to": to_provider,
        "model": model, "exec_followed": 0,
        "continuation_required": len(items), "message": _CONTINUATION_REQUIRED,
        "history_preserved": True,
    }


def describe_follow(report: Dict) -> str:
    count = report.get("continuation_required", len(report.get("items") or []))
    if not count:
        return "没有发现需要创建兼容续接任务的近期任务。"
    return "发现 %d 个既有任务需要在目标服务商下创建兼容续接任务；原历史未被改写。" % count


def describe(report: Dict) -> str:
    items = report.get("items") or []
    if not items:
        return "没有发现服务商绑定异常。"
    lines = ["发现 %d 个任务存在模型与服务商不一致。" % len(items)]
    for item in items[:20]:
        lines.append("  %s  %s → %s  （%s）" % (
            item["id"], item["from"], item["to"], item["model"]))
    lines.append(_CONTINUATION_REQUIRED)
    return "\n".join(lines)


# ---------------------------------------------------------------- 后台巡检

_LAST_CHECK = 0.0


def watch_once(min_interval: float = 3.0) -> Optional[Dict]:
    """Give the bridge a rate-limited, read-only binding diagnostic."""
    global _LAST_CHECK
    now = time.time()
    if now - _LAST_CHECK < min_interval:
        return None
    _LAST_CHECK = now
    try:
        report = repair(dry_run=True)
    except Exception as error:  # noqa: BLE001 - 巡检不能把主服务带崩
        return {"error": "%s: %s" % (type(error).__name__, error), "fixed": 0, "items": []}
    return report


def log_watch(report: Dict, log_path: Optional[Path] = None) -> None:
    if not report or not report.get("fixed"):
        return
    target = log_path or (paths.state_dir() / "threads.log")
    try:
        paths.ensure_dir(target.parent)
        with target.open("a", encoding="utf-8") as stream:
            stream.write("[%s] 自动修复 %d 个任务\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), report["fixed"]))
            for item in report["items"]:
                stream.write("    %s  %s → %s  （%s）\n" % (
                    item["id"], item["from"], item["to"], item["model"]))
    except OSError:
        pass
