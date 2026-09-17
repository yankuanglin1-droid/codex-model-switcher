"""上下文窗口守卫 —— 防止切到第三方模型后陷入「反复压缩」。

为什么需要这个东西
------------------
第三方模型在 ``model_catalog_json`` 里声明的上下文窗口，往往远小于官方模型。
例如 ``deepseek-flash`` 声明 131072，Codex 按
``effective_context_window_percent = 95`` 算出可用窗口：

    131072 × 0.95 = 124518

本机实测（2026-09-17，会话 01a0a8cd）发生的事情是这样的：

    1. 会话在官方模型上跑大，回放量稳定在 36 万 token；
    2. 切到第三方模型后，可用窗口只剩 124518，
       于是每一轮 Codex 都判定「超出预算」→ 触发压缩；
    3. 压缩后主对话确实降到 3 万 token，**但下一次请求又回到 36 万**；
    4. 17 分钟内触发了 **199 次压缩**，平均每 15~20 秒一次，
       期间模型完全没有推进任务，只是在不停地把历史压了又压。

为什么压不下去
--------------
看压缩事件本身就知道。每一次 ``compacted`` 会往 rollout 里写一条约 1MB 的
记录，其中 ``guardian_history``（守护模型拿到的完整历史快照）单独就占
902KB：

    message（摘要）            5.7 KB
    replacement_history       41.8 KB   ← 真正留给下一轮的历史，很小
    guardian_history         902.9 KB   ← 压缩顺带存档的完整快照

也就是说，**压缩产物本身比目标模型的可用窗口还大**。下一轮请求把这条
900KB 的记录一起回放，体量又回到 36 万，于是再次超限、再次压缩。
这是一个自我喂养的死循环，不是「历史太长」这么简单。

顺带一提，那条 900KB 的存档也是会话文件膨胀到 269MB 的原因
（199 × 1MB）。

结论：只靠「多压几次」永远出不来。必须让**会话体量**和**目标模型窗口**
在切换的那一刻就匹配上。

守卫做两件事
------------
1. **切换前体检**：算出「会话体量 ÷ 目标模型可用窗口」的比值。超限就明确
   要求「分叉 / 新开任务」，而不是让它原地续接后反复压缩。
2. **给 Codex 一个明确的压缩触发点**：写 ``model_auto_compact_token_limit``
   进 config.toml。Codex 原生支持这个键，给它一个**留有余量**的触发点，
   压缩后的历史才装得下，才不会压完又超、超限又压。

设计原则：宁可早压、不可压不住。所有估算在拿不到精确值时一律取保守值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 从文件尾部读取多少字节来找 token_count 事件。
# 会话文件动辄几百 MB，全量读一遍不现实；token_count 事件很密集，
# 尾部 2MB 足够覆盖最近若干轮。
TAIL_BYTES = 2 * 1024 * 1024

# 拿不到 token_count 时的兜底：按字节数粗估。
# 中英混排的 JSON 大约 3~4 字节一个 token，取 3.2 偏保守（估大一点）。
BYTES_PER_TOKEN = 3.2

# 压缩触发点占可用窗口的比例。
# 压完的历史通常能降到原来的 1/3 左右；留到 60% 就触发，
# 压完有充足余量，不会「压完还是超」。
AUTO_COMPACT_RATIO = 0.6

# 压缩触发点的下限，防止窗口极小的模型算出荒谬的值。
MIN_AUTO_COMPACT_LIMIT = 16000

# 判定阈值：会话体量 / 可用窗口
FIT_RATIO = 0.6          # 低于这个比值，直接续接没问题
TIGHT_RATIO = 1.0        # 高于 1.0 就是装不下，必须分叉

DEFAULT_EFFECTIVE_PERCENT = 95


# ---------------------------------------------------------------- 有效窗口


def effective_window(entry: Dict) -> int:
    """按 catalog 条目算出 Codex 实际认可的可用窗口。

    Codex 的逻辑是 ``context_window × effective_context_window_percent``，
    实测与它上报的 ``model_context_window`` 完全一致（131072×0.95=124518）。
    """
    context = entry.get("context_window")
    try:
        context = int(context or 0)
    except (TypeError, ValueError):
        return 0
    if context <= 0:
        return 0
    percent = entry.get("effective_context_window_percent")
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        percent = DEFAULT_EFFECTIVE_PERCENT
    if percent <= 0 or percent > 100:
        percent = DEFAULT_EFFECTIVE_PERCENT
    return int(context * percent / 100)


def window_for_model(catalog_path, model_id: str) -> Optional[int]:
    """从 catalog 文件里取某个模型的可用窗口。读不到就返回 None（未知）。"""
    try:
        document = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    target = (model_id or "").strip().lower()
    for entry in document.get("models", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("slug", "")).strip().lower() == target:
            window = effective_window(entry)
            return window or None
    return None


def model_window(catalog_path, model_id: str) -> Optional[Dict]:
    """一次性取回某个模型的窗口信息，省得重复读文件。

    返回 {"context_window": int, "percent": float, "effective": int}，
    查不到就返回 None。
    """
    try:
        document = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    target = (model_id or "").strip().lower()
    for entry in document.get("models", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("slug", "")).strip().lower() == target:
            context = entry.get("context_window")
            try:
                context = int(context or 0)
            except (TypeError, ValueError):
                return None
            if context <= 0:
                return None
            percent = entry.get("effective_context_window_percent")
            try:
                percent = float(percent)
            except (TypeError, ValueError):
                percent = DEFAULT_EFFECTIVE_PERCENT
            return {"context_window": context, "percent": percent,
                    "effective": effective_window(entry)}
    return None


def auto_compact_limit(window: int, ratio: float = AUTO_COMPACT_RATIO) -> int:
    """给 Codex 的压缩触发点。

    为什么不直接用可用窗口：压缩本身要占输出额度，压完的历史还要能装回去。
    取窗口的 60% 触发，压完才有落脚的地方。
    """
    try:
        window = int(window or 0)
    except (TypeError, ValueError):
        return MIN_AUTO_COMPACT_LIMIT
    if window <= 0:
        return MIN_AUTO_COMPACT_LIMIT
    return max(MIN_AUTO_COMPACT_LIMIT, int(window * ratio))


# ---------------------------------------------------------------- 会话体量


def _tail_text(path: Path, size: int = TAIL_BYTES) -> str:
    """只读文件尾部，并丢掉第一行（多半是被截断的半行）。"""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - size)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if start > 0:
        newline = text.find("\n")
        text = text[newline + 1:] if newline >= 0 else ""
    return text


def estimate_thread_tokens(path) -> Tuple[int, str]:
    """估算一个会话当前回放的上下文体量。返回 (token 数, 来源)。

    优先用 rollout 里 Codex 自己写的 ``token_count`` 事件（精确值）。
    拿不到再按文件字节数粗估。
    """
    path = Path(path)
    if not path.exists():
        return 0, "missing"

    samples: List[int] = []
    for line in _tail_text(path).split("\n"):
        if "token_count" not in line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        info = (record.get("payload") or {}).get("info") or {}
        usage = info.get("last_token_usage") or {}
        try:
            value = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            samples.append(value)

    if samples:
        # 取最近一次非零值；它就是 Codex 拿去和窗口比的那个数
        return samples[-1], "token_count"

    try:
        size = path.stat().st_size
    except OSError:
        return 0, "missing"
    return int(size / BYTES_PER_TOKEN), "bytes"


# ---------------------------------------------------------------- 体检


def assess(path, model_id: str, catalog_path=None, window: Optional[int] = None) -> Dict:
    """判断一个会话能不能在目标模型上原地续接。

    window 不传就从 catalog_path 里查；两者都没有就返回 unknown，
    绝不猜测、绝不谎报"没问题"。
    """
    path = Path(path)
    if window is None and catalog_path is not None:
        window = window_for_model(catalog_path, model_id)

    tokens, source = estimate_thread_tokens(path)

    report: Dict = {
        "path": str(path),
        "model": model_id,
        "exists": path.exists(),
        "tokens": tokens,
        "tokens_source": source,
        "window": window,
        "ratio": None,
        "fits": None,
        "action": "unknown",
        "auto_compact_limit": auto_compact_limit(window) if window else None,
    }
    if not window or tokens <= 0:
        return report

    ratio = tokens / float(window)
    report["ratio"] = round(ratio, 3)
    report["fits"] = ratio <= TIGHT_RATIO
    if ratio <= FIT_RATIO:
        report["action"] = "continue"
    elif ratio <= TIGHT_RATIO:
        report["action"] = "compact-first"
    else:
        report["action"] = "fork"
    return report


ACTION_TEXT = {
    "continue": "可以直接在这个对话里继续",
    "compact-first": "续接前先手动压缩一次，再继续",
    "fork": "装不下，必须分叉或新开任务，不要原地续接",
    "unknown": "窗口未知，无法判断（不会阻止你切换）",
}


def describe(report: Dict) -> str:
    """把体检结果说成人话。"""
    lines = ["会话体量：%s tokens" % format(int(report.get("tokens") or 0), ",")]
    window = report.get("window")
    if window:
        lines.append("目标模型可用窗口：%s tokens" % format(int(window), ","))
        limit = report.get("auto_compact_limit")
        if limit:
            lines.append("切换时会写入的压缩触发点：%s tokens" % format(int(limit), ","))
    ratio = report.get("ratio")
    if ratio is not None:
        lines.append("占用：%.0f%%" % (ratio * 100))
    lines.append("建议：%s" % ACTION_TEXT.get(report.get("action"), ""))
    return "\n".join(lines)


def latest_rollouts(limit: int = 3) -> List[Path]:
    """取最近修改的几个会话文件。

    会话目录按 年/月/日 分层，总量 30GB+。这里只往回看最近几天的目录，
    不做全量递归，避免为了一条提示把整盘扫一遍。
    """
    from . import paths

    root = paths.sessions_dir()
    if not root.exists():
        return []
    day_dirs: List[Path] = []
    for year in sorted(root.iterdir(), reverse=True):
        if not year.is_dir():
            continue
        for month in sorted(year.iterdir(), reverse=True):
            if not month.is_dir():
                continue
            for day in sorted(month.iterdir(), reverse=True):
                if day.is_dir():
                    day_dirs.append(day)
            if len(day_dirs) >= 4:
                break
        if len(day_dirs) >= 4:
            break
        if len(day_dirs) >= 1 and len(day_dirs) < 4:
            continue
    files: List[Path] = []
    for day in day_dirs:
        try:
            files.extend(day.glob("rollout-*.jsonl"))
        except OSError:
            continue
        if len(files) >= limit:
            break
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def assess_latest(model_id: str, catalog_path) -> Optional[Dict]:
    """体检最近那个会话。没有会话文件就返回 None。"""
    candidates = latest_rollouts(1)
    if not candidates:
        return None
    return assess(candidates[0], model_id, catalog_path=catalog_path)
