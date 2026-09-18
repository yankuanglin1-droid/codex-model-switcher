"""命令行界面。

日常用法：
  codex-switcher            进入中文菜单
  codex-switcher app        打开图形界面（本地网页，仅本机可访问）
  codex-switcher add ...    添加平台
  codex-switcher use ...    切换平台
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import (PROJECT_URL, __version__, balance as balance_module, engine, paths, registry,
               secrets, state as state_module, threads as threads_module, usage)
from . import capabilities as capabilities_module
from . import catalog as catalog_module
from . import configfile as configfile_module
from . import contextguard as contextguard_module
from .capabilities import EFFORT_TEXT
from .discovery import DiscoveryError


def out(message: str = "") -> None:
    print(message)


def fail(message: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _read_key(args) -> Optional[str]:
    if getattr(args, "key_stdin", False):
        return sys.stdin.read().strip()
    return getattr(args, "key", None)


# --------------------------------------------------------------------- 子命令

def cmd_init(args) -> int:
    from . import install
    report = install.bootstrap()
    out("已就绪：")
    out("  凭据助手：%s" % report["helper"])
    out("  密钥存储：%s" % report["secret_backend"])
    out("  配置目录：%s" % report["state_dir"])
    if report.get("codex_version"):
        out("  检测到 Codex：%s" % report["codex_version"])
    else:
        out("  ⚠ 没有检测到 codex 命令，请先安装 Codex CLI 或桌面版")
    out("")
    out("接下来运行 `codex-switcher add` 添加平台，或 `codex-switcher app` 打开图形界面。")
    return 0


def cmd_presets(args) -> int:
    for item in registry.preset_list():
        mark = "有额度接口" if item["has_balance_api"] else "无额度接口"
        out("%-12s %-22s %-52s %s" % (item["id"], item["label"], item["base_url"] or "(自填)", mark))
    return 0


def cmd_add(args) -> int:
    preset = registry.preset(args.preset) if args.preset else None
    if args.preset and not preset:
        fail("没有这个预设：%s（用 --list-presets 查看）" % args.preset)

    label = args.name or (preset or {}).get("label") or args.id
    base_url = args.base_url or (preset or {}).get("base_url") or ""
    models_url = args.models_url or (preset or {}).get("models_url") or ""

    if not base_url:
        fail("缺少 Base URL：请用 --base-url 指定，或选择一个预设（--preset）")

    extra_models = []
    for item in args.model or []:
        extra_models.extend(part.strip() for part in item.split(",") if part.strip())

    balance_spec = (preset or {}).get("balance")
    if args.balance_url:
        balance_spec = {
            "kind": "json_path",
            "url": args.balance_url,
            "value_path": args.balance_path or "",
            "currency_path": args.balance_currency_path or "",
            "currency": args.balance_currency or "",
            "label": args.balance_label or "余额",
        }

    keeps_key = (preset or {}).get("requires_key", True)
    if args.no_key:
        keeps_key = False

    result = engine.add_provider(
        provider_id=args.id or (preset or {}).get("id"),
        label=label,
        base_url=base_url,
        models_url=models_url,
        transport=args.transport or (preset or {}).get("transport", "auto"),
        api_key=_read_key(args),
        preset_id=(preset or {}).get("id"),
        balance=balance_spec,
        console_url=args.console_url or (preset or {}).get("console_url"),
        requires_key=keeps_key,
        notes=(preset or {}).get("notes"),
        manual_models=extra_models,
        auto_discover=not args.no_discover,
        switch_now=args.use,
        default_reasoning_effort=args.reasoning_effort,
    )
    record = result["record"]
    out("已添加平台：%s（%s）" % (record["label"], record["id"]))
    if record.get("transport") == "native":
        out("连接方式：平台自带 Responses 接口，直连。")
    else:
        out("连接方式：平台只支持 Chat Completions，已配置本地协议桥。")
        out("  使用前请保持协议桥运行：codex-switcher bridge")
    if result["discovery_error"]:
        out("⚠ 自动获取模型失败：%s" % result["discovery_error"])
        if not result["models"]:
            out("  可以稍后用 `codex-switcher add-model %s <模型名>` 手动补。" % record["id"])
    if result["models"]:
        out("已配置 %d 个模型：" % len(result["models"]))
        for item in result["models"]:
            out("  · %s" % item)
    if args.use:
        out("已切换为默认平台。请完全退出并重新打开 Codex 后生效。")
    return 0


def cmd_add_model(args) -> int:
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if not record:
        fail("没有找到平台：%s" % args.provider)
    from . import catalog as catalog_module
    models = record.get("models") or {}
    for name in args.models:
        models[name] = {"manual": True}
    record["models"] = models
    state_module.save(state)
    catalog_module.write_catalog(record["id"], record, list(models.keys()))
    out("已为 %s 添加 %d 个模型，共 %d 个。" % (record["label"], len(args.models), len(models)))
    return 0


def cmd_context(args) -> int:
    """查看或手改某个模型的上下文窗口。"""
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if not record:
        fail("没有找到平台：%s" % args.provider)
    models = record.get("models") or {}
    if args.model not in models:
        fail("该平台没有名为 %s 的模型。可用：%s" % (args.model, "、".join(list(models)[:8]) or "（无）"))

    if args.window is None and not args.clear:
        info = contextguard_module.model_window(
            catalog_module.catalog_path(record["id"]), args.model)
        if not info:
            out("%s · %s：窗口未知（目录里没有这个模型的条目）" % (record["label"], args.model))
            return 0
        out("%s · %s" % (record["label"], args.model))
        out("  声明窗口：%s tokens" % format(int(info["context_window"]), ","))
        out("  有效窗口：%s tokens（Codex 实际按 %d%% 算）"
            % (format(int(info["effective"]), ","), int(info["percent"])))
        out("  压缩触发点：%s tokens"
            % format(contextguard_module.auto_compact_limit(info["effective"]), ","))
        return 0

    try:
        result = engine.set_context_window(args.provider, args.model,
                                           None if args.clear else args.window)
    except engine.SwitchError as exc:
        fail(str(exc))
    if args.clear:
        out("已清除 %s · %s 的手工窗口设置，恢复按模型名推断。"
            % (record["label"], args.model))
        return 0
    out("已设置 %s · %s 的上下文窗口：%s tokens"
        % (record["label"], args.model, format(result["context_window"], ",")))
    out("  有效窗口：%s tokens" % format(int(result["effective"]), ","))
    out("  压缩触发点：%s tokens" % format(int(result["auto_compact_limit"]), ","))
    out("")
    out("目录已重新生成。切到这个模型时，压缩触发点会一起写进配置。")
    return 0


def cmd_effort(args) -> int:
    """查看或设置思考强度。"""
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if not record:
        fail("没有找到平台：%s" % args.provider)
    models = record.get("models") or {}
    if args.model not in models:
        fail("该平台没有名为 %s 的模型。可用：%s" % (args.model, "、".join(list(models)[:8]) or "（无）"))

    if not args.level and not args.clear:
        info = catalog_module.model_entry(catalog_module.catalog_path(record["id"]), args.model)
        if not info:
            out("%s · %s：目录里没有这个模型的条目" % (record["label"], args.model))
            return 0
        current = (record.get("model_overrides") or {}).get(args.model, {}).get(
            "default_reasoning_level") or record.get("default_reasoning_effort") or "high"
        levels = [item.get("effort") for item in info.get("supported_reasoning_levels") or []]
        out("%s · %s" % (record["label"], args.model))
        out("  当前档位：%s（%s）" % (current, EFFORT_TEXT.get(current, current)))
        out("  可选档位：%s" % "、".join("%s(%s)" % (item, EFFORT_TEXT.get(item, item))
                                   for item in levels))
        return 0

    try:
        result = engine.set_reasoning_effort(args.provider, args.model,
                                             None if args.clear else args.level)
    except engine.SwitchError as exc:
        fail(str(exc))
    if args.clear:
        out("已清除 %s · %s 的手工档位，恢复平台默认。"
            % (record["label"], args.model))
        return 0
    out("已设置 %s · %s 的思考强度：%s"
        % (record["label"], args.model, result["label"]))
    if result.get("config_updated"):
        out("配置已同步 —— 现在正在用这个模型，改完立刻生效。")
    else:
        out("下次切到这个模型时生效。")
    return 0


def _capability_line(row: Dict) -> str:
    mark = {"yes": "支持", "no": "不支持", "unknown": "未知"}.get(row["vision"], "未知")
    reasoning = {"yes": "支持", "no": "不支持", "unknown": "未测出"}.get(row["reasoning"], "未知")
    tools = {"yes": "支持", "no": "不支持", "unknown": "未测出"}.get(row["tools"], "未知")
    source = {"verified": "实测", "measured": "本机实测", "inferred": "推断",
              "documented": "官方", "manual": "手动指定"}.get(
                  row.get("source"), row.get("source") or "")
    effort = row.get("effort") or "-"
    return "  %-28s 读图%-4s 思考%-4s 工具%-4s 档位%-8s %s" % (
        row["model"], mark, reasoning, tools, effort, source)


def cmd_capabilities(args) -> int:
    """能力矩阵：这个平台的模型到底支持什么。"""
    if args.probe and not args.provider:
        fail("实测需要指定平台，例如：codex-switcher capabilities deepseek --probe --apply")
    if args.probe:
        try:
            result = engine.probe_capabilities(args.provider, args.model,
                                               apply_result=args.apply)
        except engine.SwitchError as exc:
            fail(str(exc))
        out("实测 %s（%s）" % (result["provider"],
                            "直连 Responses" if result.get("transport") == "native" else "经协议桥"))
        for item in result["results"]:
            if item.get("error"):
                out("  %-28s 探测失败：%s" % (item["model"], item["error"]))
                continue
            out("  %-28s 读图%-4s 思考%-4s 工具%-4s" % (
                item["model"],
                {"yes": "支持", "no": "不支持", "unknown": "未测出"}[item["vision"]],
                {"yes": "支持", "no": "不支持", "unknown": "未测出"}[item["reasoning"]],
                {"yes": "支持", "no": "不支持", "unknown": "未测出"}[item["tools"]]))
            for key, value in (item.get("evidence") or {}).items():
                out("      · %s：%s" % (key, value))
            if args.apply and (item.get("applied") or {}).get("changed"):
                out("      已写回目录：%s" % "、".join(item["applied"]["changed"]))
        if not args.apply:
            out("")
            out("加 --apply 可以把这些结论写进目录，Codex 之后就按真实能力发请求。")
        return 0

    rows = engine.capability_matrix(args.provider)
    if not rows:
        out("还没有可分析的模型。先运行 `codex-switcher refresh <平台>` 拉一次模型列表。")
        return 0
    provider = None
    for row in rows:
        if row["provider"] != provider:
            provider = row["provider"]
            out("%s" % row["provider_label"])
        out(_capability_line(row))
    out("")
    out("读图 / 思考 / 工具三列：实测 = 拿真实请求测过，推断 = 只按模型名猜的。")
    out("想拿准数：`codex-switcher capabilities <平台> --probe --apply`（会消耗少量 token）。")
    out("注意：%s 只有 OpenAI 自己提供，第三方平台一律没有。"
        % "、".join(capabilities_module.OPENAI_ONLY_TOOLS.values()))
    out("另外：%s" % capabilities_module.GENERATION_NOTE)
    return 0


def cmd_list(args) -> int:
    overview = engine.provider_overview()
    current = engine.current_status()
    out("当前默认：%s · %s" % (current.get("model_provider", "openai"), current.get("model") or "默认"))
    out("")
    if not overview:
        out("还没有添加任何第三方平台。运行 `codex-switcher add --preset deepseek --key-stdin` 试试。")
        return 0
    for item in overview:
        mark = "●" if item["is_current"] else "○"
        out("%s %-14s %-22s 模型 %-3d 密钥 %s" % (
            mark, item["id"], item["label"] or "", len(item["models"]), item["key_hint"]))
    return 0


def cmd_models(args) -> int:
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if not record:
        fail("没有找到平台：%s" % args.provider)
    models = list((record.get("models") or {}).keys())
    if not models:
        out("该平台还没有模型。运行 `codex-switcher refresh %s` 重新拉取。" % args.provider)
        return 0
    for index, name in enumerate(models, 1):
        out("%2d. %s" % (index, name))
    return 0


def cmd_bridge(args) -> int:
    from . import bridge
    if args.install_agent:
        from . import install
        target = install.install_launch_agent(port=args.port)
        out("已安装后台服务：%s" % target)
        out("协议桥会随登录自动启动，并在退出后自动重启。")
        return 0
    if args.uninstall_agent:
        from . import install
        removed = install.remove_launch_agent()
        out("已移除后台服务。" if removed else "没有找到已安装的后台服务。")
        return 0
    if bridge.is_running(args.port):
        out("协议桥已经在运行：http://127.0.0.1:%d" % args.port)
        return 0
    return bridge.run(port=args.port)


def _pid_is_ours(pid: int) -> bool:
    """确认这个进程号确实是本工具起的服务，避免误杀复用了同一 PID 的其它程序。"""
    from . import platform_compat
    return platform_compat.is_our_process(pid)


def cmd_stop(args) -> int:
    """停掉后台的图形界面与协议桥。"""
    targets = [("图形界面", paths.gui_state_file()), ("协议桥", paths.bridge_pid_file())]
    stopped, skipped = [], []
    for label, record_path in targets:
        if not record_path.exists():
            continue
        try:
            pid = int(json.loads(record_path.read_text()).get("pid") or 0)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pid = 0
        if pid <= 1:
            skipped.append("%s（记录无效）" % label)
        elif not _pid_is_ours(pid):
            skipped.append("%s（PID %d 已不属于本工具，跳过）" % (label, pid))
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append("%s（PID %d）" % (label, pid))
            except ProcessLookupError:
                stopped.append("%s（已经不在运行）" % label)
            except PermissionError:
                skipped.append("%s（没有权限结束 PID %d）" % (label, pid))
        try:
            record_path.unlink()
        except OSError:
            pass

    if not stopped and not skipped:
        out("没有正在运行的后台服务。")
        return 0
    for item in stopped:
        out("已停止：%s" % item)
    for item in skipped:
        out("已跳过：%s" % item)
    return 0


def cmd_tasks(args) -> int:
    """列出任务，并标出服务商和模型对不上的那些。"""
    info = threads_module.summarize()
    out("任务总数：%d" % info["total"])
    if not info["mismatch"]:
        out("服务商绑定全部正常。")
        return 0
    out("发现 %d 个任务的服务商和模型对不上（在这些任务里换模型会报 model is not supported）：" % info["mismatch"])
    out("")
    for item in info["items"]:
        out("  %s  %-24s %s → %s" % (item["id"][:8], item["model"], item["provider"], item["expected"]))
        out("      %s" % item["title"][:50])
    out("")
    out("修复：codex-switcher repair           （先看预演加 --dry-run）")
    return 1


def cmd_repair(args) -> int:
    # --follow：把最近在用的任务搬到当前平台上。用于「直接在 config.toml 里
    # 改了服务商」或者切换时 Codex 正在写文件、搬家被跳过的场合。
    if getattr(args, "follow", False):
        try:
            current = configfile_module.read_top_level(
                paths.config_path().read_text(), ["model_provider", "model"])
        except OSError as exc:
            fail("读不到 Codex 配置：%s" % exc)
        target = current.get("model_provider")
        model = current.get("model")
        if not target:
            fail("配置里没有 model_provider，先用 codex-switcher use 切一次。")
        moved_total = 0
        for provider_id in (state_module.load().get("providers") or {}):
            if provider_id == target or provider_id == "openai":
                continue
            report = threads_module.follow_switch(
                provider_id, target, model, dry_run=args.dry_run)
            if report.get("items"):
                out(threads_module.describe_follow(report))
                moved_total += report.get("moved", 0)
        if not moved_total and not args.dry_run:
            out("没有需要搬的任务。")
        elif not args.dry_run:
            out("")
            out("请完全退出并重新打开 Codex，让改动生效。")
        return 0
    try:
        report = threads_module.repair(thread_id=args.thread, dry_run=args.dry_run,
                                       deep=getattr(args, "deep", False))
    except threads_module.ThreadError as exc:
        fail("修复失败：%s" % exc)
    out(threads_module.describe(report))
    if report.get("fixed") and not args.dry_run:
        out("")
        out("请完全退出并重新打开 Codex，让改动生效。")
    return 0


def cmd_history(args) -> int:
    """检查/清洗会话历史里会让第三方平台拒绝请求的条目。"""
    from . import history as history_module

    # 全量清扫：不设窗口，把**所有**会话文件过一遍。
    # 为什么需要它：缺 call_id 的孤儿工具结果不挑新旧，最老的可能躺在几个月
    # 前的小文件里；只扫"最近 N 个"永远扫不到它，用户哪天点开那个对话就炸。
    if getattr(args, "sweep", False):
        official = engine.current_status().get("model_provider") == engine.OFFICIAL_PROVIDER
        cross = getattr(args, "cross_provider", False)
        if not args.dry_run:
            out("正在全量清扫（首次要把所有会话文件读一遍，可能要几十秒）…")
        report = history_module.sweep_all(moving_off_openai=not official,
                                          cross_provider=cross,
                                          dry_run=args.dry_run)
        out(history_module.describe_sweep(report))
        for item in (report.get("items") or [])[:5]:
            out("  %s" % Path(item["path"]).name)
        if len(report.get("items") or []) > 5:
            out("  … 还有 %d 个" % (len(report["items"]) - 5))
        return 0

    targets: List[Path] = []
    if args.thread:
        try:
            items = threads_module.list_threads()
        except threads_module.ThreadError as exc:
            fail("读不到任务列表：%s" % exc)
        matched = [i for i in items if str(i["id"]).startswith(args.thread)]
        if not matched:
            fail("找不到任务：%s" % args.thread)
        for item in matched:
            if item.get("rollout_path"):
                targets.append(Path(item["rollout_path"]))
    else:
        targets = history_module.recent_rollouts(0 if args.all else args.scan)

    cross = getattr(args, "cross_provider", False)
    if not args.clean:
        report = {"scanned": 0, "dirty": [],
                  "totals": {"orphan_outputs": 0, "openai_only": 0, "cross_provider": 0}}
        for path in targets:
            report["scanned"] += 1
            info = history_module.inspect(path)
            if history_module.is_dirty(info, cross_provider=cross):
                report["dirty"].append(info)
                report["totals"]["orphan_outputs"] += info["orphan_outputs"]
                report["totals"]["openai_only"] += info["openai_only"]
                report["totals"]["cross_provider"] += info["cross_provider"]
        out("检查了最近 %d 个会话文件" % report["scanned"])
        if not report["dirty"]:
            out("没有发现问题条目 ✅")
            return 0
        out("")
        out("发现 %d 个会话含有会让平台拒绝请求的条目：" % len(report["dirty"]))
        out("  · 缺 call_id 的工具结果：%d 条（会直接 400：missing field `call_id`）"
            % report["totals"]["orphan_outputs"])
        out("  · OpenAI 专有推理条目：%d 条（换平台后无意义，第三方不认）"
            % report["totals"]["openai_only"])
        if report["totals"]["cross_provider"]:
            out("  · OpenAI 服务端工具条目：%d 条（web_search / computer / 图像等，"
                "第三方不认，会导致整个请求被拒）" % report["totals"]["cross_provider"])
            out("    要把会话搬到第三方继续，加 --cross-provider 清理")
        for info in report["dirty"][:5]:
            out("    %s" % Path(info["path"]).name)
        if len(report["dirty"]) > 5:
            out("    … 还有 %d 个" % (len(report["dirty"]) - 5))
        out("")
        out("全量清理：codex-switcher history --sweep   （所有会话，会先备份，推荐）")
        out("只清最近：codex-switcher history --clean   （会先备份）")
        out("预演：    codex-switcher history --sweep --dry-run")
        return 0

    official = engine.current_status().get("model_provider") == engine.OFFICIAL_PROVIDER
    report = history_module.clean(targets, moving_off_openai=not official,
                                  dry_run=args.dry_run, cross_provider=cross)
    if args.dry_run:
        out("预演：会清理 %d 个会话文件" % report["changed"])
    else:
        out("已清理 %d 个会话文件" % report["changed"])
        if report["removed"]["orphan_outputs"] or report["removed"]["openai_only"]:
            out("  删掉缺 call_id 的工具结果：%d 条" % report["removed"]["orphan_outputs"])
            out("  删掉 OpenAI 专有推理条目：%d 条" % report["removed"]["openai_only"])
        if report["removed"]["cross_provider"]:
            out("  成对剥离 OpenAI 服务端工具条目：%d 条（现在这个会话可以搬到第三方继续了）"
                % report["removed"]["cross_provider"])
        if report.get("backup_dir"):
            out("  备份：%s" % report["backup_dir"])
    if not report["changed"]:
        out("没有需要清理的内容。")
    if report.get("skipped_active"):
        out("")
        out("跳过了 %d 个最近还在写入的会话（多半是你正开着的对话）。"
            % len(report["skipped_active"]))
        out("完全退出 Codex 之后再跑一次这个命令即可处理它们。")
    return 0


def _catalog_for(model: str, provider: Optional[str] = None) -> Optional[str]:
    """找到声明了某个模型的 catalog 文件。

    第三方模型都挂在各自的平台目录下；一个模型可能只属于其中一个平台，
    所以按平台找不到时要扫一遍，别轻易判"窗口未知"。
    """
    if provider:
        candidate = catalog_module.catalog_path(provider)
        if Path(candidate).exists() and contextguard_module.window_for_model(candidate, model):
            return str(candidate)
    root = paths.catalog_dir()
    if not root.exists():
        return None
    for item in sorted(root.glob("*.json")):
        if contextguard_module.window_for_model(item, model):
            return str(item)
    return None


def _guard_target(model: Optional[str], catalog_path: Optional[str]):
    """体检谁：目标模型 + 它的 catalog + 最近那个会话。三者缺一就返回 None。"""
    status = engine.current_status()
    model = model or status.get("model")
    if not model:
        return None
    if not catalog_path:
        catalog_path = _catalog_for(model, status.get("model_provider"))
    candidates = contextguard_module.latest_rollouts(1)
    if not candidates:
        return None
    return model, catalog_path, candidates[0]


def guard_report(model: Optional[str] = None, catalog_path: Optional[str] = None,
                 thread=None) -> Optional[Dict]:
    """对最近那个会话做一次体量体检。拿不齐信息就返回 None（不猜）。"""
    if thread is None:
        resolved = _guard_target(model, catalog_path)
        if resolved is None:
            return None
        model, catalog_path, thread = resolved
    return contextguard_module.assess(thread, model, catalog_path=catalog_path)


def describe_guard(report: Dict) -> List[str]:
    """把体检结果翻成可以给用户的几行提示。"""
    lines = [contextguard_module.describe(report)]
    if report.get("action") == "fork":
        lines.append("")
        lines.append("这时候直接续接会触发自动压缩，而压缩产物本身就超过窗口，"
                     "结果是压完还超、超限又压 —— 除了烧 token 什么也不会发生。")
        lines.append("请新开一个任务，或先对这个会话用「分叉」再换模型。")
    elif report.get("action") == "compact-first":
        lines.append("")
        lines.append("体量已经贴着窗口上限了。续接前先在对话里手动压缩一次，再继续。")
    return lines


def cmd_guard(args) -> int:
    """会话体量体检：这个对话搬到目标模型上装得下吗。"""
    status = engine.current_status()
    thread = Path(args.thread) if getattr(args, "thread", None) else None
    if thread is None:
        resolved = _guard_target(getattr(args, "model", None), None)
        if resolved is None:
            fail("拿不到当前默认模型或会话文件。可以显式指定："
                 "codex-switcher guard --model deepseek-flash --thread <会话文件路径>")
        model, catalog_path, thread = resolved
    else:
        model = getattr(args, "model", None) or status.get("model")
        if not model:
            fail("请用 --model 指定目标模型，例如 --model deepseek-flash")
        # 指定了会话文件也要按目标模型查窗口，否则只能给出"未知"
        catalog_path = _catalog_for(model, getattr(args, "provider", None))

    report = contextguard_module.assess(thread, model, catalog_path=catalog_path)
    out("会话：%s" % report["path"])
    for line in describe_guard(report):
        out(line)
    return 0


def cmd_use(args) -> int:
    try:
        result = engine.switch_to(args.provider, args.model, dry_run=args.dry_run)
    except engine.SwitchError as exc:
        fail("切换失败：%s" % exc)
    except Exception as exc:  # noqa: BLE001
        fail("切换失败：%s" % exc)
    if result.get("dry_run"):
        out("预演通过：可以切换到 %s · %s" % (result["provider"], result["model"]))
        return 0
    out("已切换为 %s · %s" % (result.get("label", result["provider"]), result["model"]))
    out("配置备份：%s" % result["backup"])
    out("")
    out("接下来：完全退出（⌘Q）并重新打开 Codex，然后新建任务。")
    # 后台全量迁移在 CLI 里必须同步跑：daemon 线程会随进程退出被杀，
    # 排队了却没跑等于没搬。GUI 服务常驻才用得上后台线程。
    follow_report = result.get("threads_followed") or {}
    if (result.get("full_follow") or {}).get("scheduled") and follow_report.get("to"):
        out("")
        out("正在把 %s 平台的全部任务（含每日定时任务）搬到 %s，任务多时会花一点时间…"
            % (follow_report.get("from"), follow_report.get("to")))
        totals = engine.full_follow_all(follow_report["from"], follow_report["to"],
                                        follow_report.get("model"))
        out("任务迁移完成：%d 个任务已搬到 %s（含定时任务 %d 个）。"
            % (totals["moved"], follow_report["to"], totals["exec_followed"]))
    if result["provider"] != engine.OFFICIAL_PROVIDER:
        # 跨平台自动清洗：别家产生的服务端工具条目（web_search_call 等）
        # 回放到新平台必然 400，切换时已经自动剥掉（先备份）。这里报告结果。
        cleaned = result.get("history_clean") or {}
        removed = (cleaned.get("removed") or {}).get("cross_provider", 0) \
            + (cleaned.get("removed") or {}).get("orphan_outputs", 0)
        if cleaned.get("cleaned"):
            out("旧会话体检：已自动清洗 %d 份会话，剥掉 %d 条跨平台不认的条目（有备份）。"
                % (cleaned["cleaned"], removed))
            if cleaned.get("budget_exhausted"):
                out("           会话太多，这次只清了最近的一部分，下次切换会接着清。")
        elif cleaned.get("skipped_active"):
            out("旧会话体检：有 %d 份会话正在使用中，这次没动；"
                "下次切换（或 codex-switcher history --clean）会再处理。"
                % cleaned["skipped_active"])
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if record and engine.resolve_transport(record) == "bridge":
        port = int(record.get("bridge_port") or 8787)
        if not engine.bridge_module.is_running(port):
            out("")
            out("⚠ 这个平台走本地协议桥，但桥还没在运行。先执行：codex-switcher bridge")
    out("任务绑定：最近在用的旧任务、每日定时任务，以及其余任务（后台分批）"
        "都已经或正在搬到 %s，旧对话可以直接继续。" % result.get("label", result["provider"]))

    # 切到窗口更小的第三方模型时，旧会话可能根本装不下。
    # 装不下又不说，用户就会看到「反复压缩」：压完还超、超限又压。
    if result.get("provider") != engine.OFFICIAL_PROVIDER:
        try:
            report = guard_report(result.get("model"),
                                  _catalog_for(result.get("model"), result.get("provider")))
        except Exception:  # noqa: BLE001 - 体检失败不能影响切换本身
            report = None
        if report and report.get("action") in ("fork", "compact-first"):
            out("")
            out("⚠ 最近的那个对话在 %s 上装不下：" % result.get("model"))
            for line in describe_guard(report):
                out("  " + line.replace("\n", "\n  "))
            out("")
            out("  想随时复查：codex-switcher guard")

    # 切完当场验链路。以前切完只说"成功"，可配置写对了不代表能发请求 ——
    # 凭据助手跑不起来时，用户要等到 Codex 里一直重连、甚至报
    # 「ChatGPT 订阅无法使用第三方模型」才知道，而那时已经无从下手。
    try:
        from . import readiness
        report = readiness.check(provider_id=result.get("provider"))
    except Exception:  # noqa: BLE001 - 验不了也不能让切换报失败
        report = None
    if report is not None and not report.get("ok"):
        out("")
        out("⚠ 切换写好了，但现在还发不出请求：")
        for line in readiness.to_text(report).splitlines():
            if line.strip().startswith(("✗", "!")):
                out("  " + line.strip())
        out("")
        out("  详细排查：codex-switcher check")
    return 0


def cmd_restore(args) -> int:
    model = args.model or engine.DEFAULT_OFFICIAL_MODEL
    result = engine.switch_to(engine.OFFICIAL_PROVIDER, model, dry_run=args.dry_run)
    if result.get("dry_run"):
        out("预演通过：可以恢复官方 OpenAI · %s" % result["model"])
        return 0
    out("已恢复官方 OpenAI 登录 · %s" % result["model"])
    out("配置备份：%s" % result["backup"])
    # CLI 里同步跑全量迁移（daemon 线程会随进程退出被杀，理由同 cmd_use）
    follow_report = result.get("threads_followed") or {}
    if (result.get("full_follow") or {}).get("scheduled") and follow_report.get("to"):
        out("正在把 %s 平台的全部任务搬回官方，任务多时会花一点时间…"
            % follow_report.get("from"))
        totals = engine.full_follow_all(follow_report["from"], follow_report["to"],
                                        follow_report.get("model"))
        out("任务迁移完成：%d 个任务已搬回官方（含定时任务 %d 个）。"
            % (totals["moved"], totals["exec_followed"]))
    out("完全退出并重新打开 Codex 后生效。")
    return 0


def cmd_balance(args) -> int:
    state = state_module.load()
    providers = state.get("providers") or {}
    targets = [args.provider] if args.provider else list(providers)
    if not targets:
        out("还没有添加平台。")
        return 0
    local = usage.local_usage() if not args.no_local else {}
    for provider_id in targets:
        record = providers.get(provider_id)
        if not record:
            out("%s：没有找到" % provider_id)
            continue
        result = balance_module.query(record, secrets.load(provider_id))
        out("【%s】%s" % (record.get("label"), result.get("display", "")))
        if result.get("message"):
            out("  %s" % result["message"])
        for field in result.get("fields") or []:
            out("  · %s：%s" % (field["label"], field["value"]))
        if not args.no_local:
            stat = local.get(provider_id)
            if stat:
                out("  · 本机用量：%d 个任务 / %s tokens" % (
                    stat["sessions"], usage.human_tokens(stat["total_tokens"])))
        if result.get("console_url"):
            out("  · 官网查询：%s" % result["console_url"])
        out("")
    return 0


def cmd_refresh(args) -> int:
    targets: List[str] = []
    if args.provider and args.provider != "all":
        targets = [args.provider]
    else:
        targets = state_module.provider_ids(state_module.load())
    if not targets:
        out("还没有添加平台。")
        return 0
    failures = 0
    for provider_id in targets:
        try:
            result = engine.refresh_models(provider_id)
            out("【%s】已更新 %d 个模型。" % (result["record"]["label"], len(result["models"])))
        except engine.SwitchError as exc:
            failures += 1
            out("【%s】更新失败：%s" % (provider_id, exc))
    return 1 if failures and len(targets) == failures else 0


def cmd_remove(args) -> int:
    try:
        result = engine.remove_provider(args.provider, purge_key=not args.keep_key)
    except engine.SwitchError as exc:
        fail("删除失败：%s" % exc)
    out("已删除平台：%s" % result["provider"])
    if result.get("key_purged"):
        out("钥匙串里的密钥也已删除。")
    if result.get("backup"):
        out("配置备份：%s" % result["backup"])
    return 0


def cmd_status(args) -> int:
    from . import install
    from . import update as update_module
    status = engine.current_status()
    out("配置文件：%s" % status["config_path"])
    out("当前平台：%s" % status.get("model_provider", "unknown"))
    out("当前模型：%s" % (status.get("model") or "（未设置）"))
    if status.get("catalog"):
        out("模型目录：%s" % status["catalog"])
    if status.get("error"):
        out("⚠ %s" % status["error"])
    out("密钥存储：%s" % secrets.backend_label())
    version = install.codex_version()
    out("Codex 版本：%s" % (version or "未检测到"))
    out("切换器版本：v%s · %s" % (__version__, update_module.describe(update_module.check())))
    return 0


def cmd_quota(args) -> int:
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if not record:
        fail("没有找到平台：%s" % args.provider)
    if args.clear:
        engine.set_quota(args.provider, None)
        out("已清除 %s 的额度设置。" % record.get("label"))
        return 0
    if args.tokens is None:
        stat = usage.local_usage().get(args.provider)
        used = (stat or {}).get("total_tokens", 0)
        quota = record.get("quota_tokens")
        if quota:
            detail = engine.usage_with_quota(args.provider, quota, used)
            out("%s：本机已用 %s / %s tokens（%.1f%%），约剩 %s" % (
                record.get("label"), usage.human_tokens(used), usage.human_tokens(quota),
                detail["percent"], usage.human_tokens(detail["remaining_tokens"])))
        else:
            out("%s：本机已用 %s tokens（没有设置套餐额度，无法算百分比）" % (
                record.get("label"), usage.human_tokens(used)))
            out("如果套餐页写了总 token 数，可以这样设置：")
            out("  codex-switcher quota %s --tokens 500000000" % args.provider)
        return 0
    try:
        engine.set_quota(args.provider, args.tokens)
    except engine.SwitchError as exc:
        fail(str(exc))
    out("已记录 %s 的套餐额度：%s tokens。" % (record.get("label"), usage.human_tokens(args.tokens)))
    return 0


def cmd_update(args) -> int:
    from . import update as update_module
    result = update_module.check(force=True)
    out(update_module.describe(result))
    if result.get("status") != "ok" or result.get("up_to_date"):
        return 0
    if not args.pull:
        out("")
        out("更新方式（在仓库目录里）：")
        out("  git pull          # 然后重跑 bash install.sh")
        out("  或者：codex-switcher update --pull")
        return 0
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        fail("当前不是 git 仓库，无法自动更新。请重新克隆：%s" % result.get("url"))
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
    if (status.stdout or "").strip():
        fail("仓库里有未提交的改动，为避免覆盖你的修改，已停止自动更新。请先手动处理。")
    out("正在更新：%s" % repo)
    pull = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                          capture_output=True, text=True, timeout=120)
    out((pull.stdout or "").strip() or (pull.stderr or "").strip())
    if pull.returncode != 0:
        fail("更新失败，请手动执行 git pull 查看原因。")
    out("")
    out("更新完成。如果提示符没变化，重新运行一次 bash install.sh 即可。")
    return 0


def cmd_check(args) -> int:
    """端到端链路检查。

    单独成一条命令，是因为「页面加载不出来 / 反复重新连接 / 报 ChatGPT 订阅
    无法使用第三方模型」这三张脸背后是同一条链路断了，而单点检查每处都显示
    "没问题"。这条命令把整串一起验，凭据助手会真跑一遍。
    """
    from . import readiness
    report = readiness.check(provider_id=getattr(args, "provider", None) or None,
                             check_history=bool(getattr(args, "history", False)))
    out("== 链路检查 ==")
    out(readiness.to_text(report))
    out("")
    if report["ok"]:
        out("✅ " + readiness.describe(report))
        return 0
    out("❌ " + readiness.describe(report))
    return 1


def cmd_doctor(args) -> int:
    from . import install
    problems = []
    out("== 环境自检 ==")
    out("Python：%s" % sys.version.split()[0])
    if sys.version_info < (3, 11):
        try:
            import tomli  # noqa: F401
            out("  · 已安装 tomli，可用于旧版 Python")
        except ModuleNotFoundError:
            problems.append("Python 低于 3.11 且缺少 tomli，请升级 Python 或安装 tomli")
    out("密钥存储后端：%s" % secrets.backend_label())
    if secrets.backend() == "file":
        out("  · 提示：当前系统没有可用的钥匙串，密钥会以 0600 权限存到本地文件")
    helper = install.ensure_helper()
    out("凭据助手：%s" % helper)
    config = paths.config_path()
    out("Codex 配置：%s%s" % (config, "" if config.exists() else "（不存在）"))
    if config.exists():
        try:
            from . import configfile
            if configfile.toml_available():
                configfile.parse(config.read_text())
                out("  · 配置可正常解析")
            else:
                configfile.read_top_level(config.read_text(), ("model_provider",))
                out("  · 当前 Python 没有 TOML 库，改用逐行校验（建议升级到 Python 3.11+）")
        except Exception as exc:  # noqa: BLE001
            problems.append("配置文件解析失败：%s" % exc)
    else:
        problems.append("没有找到 Codex 配置文件，请先运行一次 Codex")
    version = install.codex_version()
    out("Codex 可执行文件：%s" % (version or "未检测到（可能只装了桌面版）"))

    overview = engine.provider_overview()
    out("已配置平台：%d 个" % len(overview))
    for item in overview:
        note = []
        if item["requires_key"] and not item["has_key"]:
            note.append("缺少密钥")
        if not item["models"]:
            note.append("没有模型")
        if note:
            problems.append("%s：%s" % (item["label"] or item["id"], "、".join(note)))

    bridge_items = [item for item in overview if item.get("transport") != "native"]
    if bridge_items:
        out("需要协议桥的平台：%d 个" % len(bridge_items))
        if any(item.get("bridge_running") for item in bridge_items):
            out("  · 协议桥正在运行")
        else:
            problems.append("有平台依赖本地协议桥，但桥没有运行；请执行 codex-switcher bridge "
                            "或 codex-switcher bridge --install-agent")

    if getattr(args, "history", False):
        from . import history as history_module
        scan = history_module.scan(limit=getattr(args, "scan", 30))
        out("会话历史：扫了最近 %d 个文件" % scan["scanned"])
        if scan["dirty"]:
            out("  · 含缺 call_id 的工具结果：%d 条" % scan["totals"]["orphan_outputs"])
            out("  · 含 OpenAI 专有推理条目：%d 条" % scan["totals"]["openai_only"])
            problems.append(
                "有 %d 个会话的历史会被第三方平台拒绝（报 missing field `call_id`）；"
                "执行 codex-switcher history --clean 清理" % len(scan["dirty"]))
        else:
            out("  · 没有发现问题条目")

    # 环境和平台列表都查完了，最后再把整条链路串起来验一遍。
    # 前面那些检查每一处单独看都可能"没问题"，但 Codex 照样发不出请求 ——
    # 「加载不出来 / 反复重连 / 报订阅无法使用第三方模型」就是这么来的。
    out("")
    out("== 链路检查 ==")
    from . import readiness
    report = readiness.check(check_history=bool(getattr(args, "history", False)))
    out(readiness.to_text(report))
    if not report["ok"]:
        problems.append(readiness.describe(report))

    out("")
    if problems:
        out("发现 %d 个问题：" % len(problems))
        for item in problems:
            out("  ✗ %s" % item)
        return 1
    out("没有发现问题。")
    return 0


def cmd_app(args) -> int:
    from .webui import server
    _ensure_bridge()
    if not getattr(args, "force_new", False):
        url = server.existing_url()
        if url:
            out("图形界面已经在运行：%s" % url)
            if not args.no_open:
                import webbrowser
                webbrowser.open(url)
            return 0
    return server.run(port=args.port, open_browser=not args.no_open)


def _ensure_bridge() -> None:
    """界面起来之前，先把需要用到本地协议桥拉起来。

    macOS 的 .app 是走 launch.sh 的，那里已经会拉桥；但 Windows 是从桌面
    快捷方式直接跑 `app`，没人管桥，走桥的平台就会连不上。
    没有平台需要桥时不白起进程。
    """
    from . import bridge, platform_compat
    try:
        overview = engine.provider_overview()
    except Exception:  # noqa: BLE001
        return
    if not any(item.get("transport") != "native" for item in overview):
        return
    try:
        if bridge.is_running():
            return
        platform_compat.spawn_detached(
            [sys.executable, "-m", "codex_switcher.bridge"],
            paths.state_dir() / "bridge.log")
    except (OSError, RuntimeError):
        # 桥起不来不该拦住界面：界面里会显示它没在运行，用户也能手动补
        pass


def cmd_export(args) -> int:
    """导出脱敏状态，方便贴到 issue 里求助。"""
    state = state_module.load()
    payload = {
        "version": __version__,
        "current": engine.current_status(),
        "providers": [
            {
                "id": item["id"],
                "label": item["label"],
                "base_url": item["base_url"],
                "wire_api": item["wire_api"],
                "model_count": len(item["models"]),
                "models": item["models"][:40],
                "has_key": item["has_key"],
            }
            for item in engine.provider_overview()
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(text + "\n")
        out("已写入 %s（不含任何密钥）" % args.output)
    else:
        out(text)
    return 0


# ------------------------------------------------------------------- 交互菜单

def _prompt(text: str, default: str = "") -> str:
    suffix = (" [%s]" % default) if default else ""
    try:
        value = input("%s%s： " % (text, suffix)).strip()
    except EOFError:
        return default
    return value or default


def _choose(prompt: str, items: List[str]) -> Optional[int]:
    for index, item in enumerate(items, 1):
        out("  %2d. %s" % (index, item))
    raw = _prompt(prompt)
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    if 1 <= number <= len(items):
        return number - 1
    return None


def interactive() -> int:
    while True:
        status = engine.current_status()
        out("")
        out("codex（ChatGPT App）多平台模型切换 v%s" % __version__)
        out("当前：%s · %s   密钥存储：%s" % (
            status.get("model_provider", "?"), status.get("model") or "-", secrets.backend_label()))
        out("")
        out("  1. 添加平台（填平台名 / ID / API Key）")
        out("  2. 切换平台与模型")
        out("  3. 查看模型列表")
        out("  4. 查看余额 / 额度")
        out("  5. 打开图形界面")
        out("  6. 恢复官方 OpenAI")
        out("  7. 环境自检")
        out("  0. 退出")
        choice = _prompt("请选择")
        if choice in ("0", "q", "Q"):
            return 0
        if choice == "1":
            wizard_add()
        elif choice == "2":
            wizard_switch()
        elif choice == "3":
            overview = engine.provider_overview()
            index = _choose("看哪个平台的模型", [i["label"] or i["id"] for i in overview])
            if index is not None:
                for name in overview[index]["models"]:
                    out("  · %s" % name)
        elif choice == "4":
            cmd_balance(argparse.Namespace(provider=None, no_local=False))
        elif choice == "5":
            cmd_app(argparse.Namespace(port=0, no_open=False))
        elif choice == "6":
            cmd_restore(argparse.Namespace(model=None, dry_run=False))
        elif choice == "7":
            cmd_doctor(argparse.Namespace())
        else:
            out("没有这个选项。")


def wizard_add() -> None:
    out("")
    out("可用的预设平台：")
    presets = registry.preset_list()
    index = _choose("选择平台（选 0 直接手填）", [p["label"] for p in presets] + ["我自己填 Base URL"])
    preset = presets[index] if index is not None and index < len(presets) else None
    if preset:
        label = preset["label"]
        base_url = preset["base_url"]
        models_url = ""
        wire_api = preset["wire_api"]
    else:
        label = _prompt("平台名称")
        base_url = _prompt("Base URL（例如 https://api.example.com/v1）")
        models_url = _prompt("模型列表地址（回车自动推断）")
        wire_api = _prompt("协议（chat 或 responses）", "chat")
    api_key = ""
    if (preset or {}).get("requires_key", True):
        api_key = _prompt("API Key（输入不会显示在屏幕上）", "")
    if not base_url:
        out("缺少 Base URL，已取消。")
        return
    try:
        result = engine.add_provider(
            provider_id=(preset or {}).get("id"),
            label=label,
            base_url=base_url,
            models_url=models_url,
            wire_api=wire_api,
            api_key=api_key or None,
            preset_id=(preset or {}).get("id"),
            balance=(preset or {}).get("balance"),
            console_url=(preset or {}).get("console_url"),
            requires_key=(preset or {}).get("requires_key", True),
            notes=(preset or {}).get("notes"),
        )
    except Exception as exc:  # noqa: BLE001
        out("添加失败：%s" % exc)
        return
    record = result["record"]
    out("已添加 %s，模型 %d 个。" % (record["label"], len(result["models"])))
    if result["discovery_error"]:
        out("⚠ %s" % result["discovery_error"])
        raw = _prompt("手动输入模型名，用逗号分隔（可留空）")
        names = [item.strip() for item in raw.split(",") if item.strip()]
        if names:
            cmd_add_model(argparse.Namespace(provider=record["id"], models=names))
    if _prompt("现在就切换到这个平台？(y/N)").lower() == "y":
        wizard_switch()


def wizard_switch() -> None:
    overview = engine.provider_overview()
    labels = ["（恢复官方 OpenAI）"] + ["%s（%d 个模型）" % (i["label"] or i["id"], len(i["models"])) for i in overview]
    index = _choose("切换到哪个平台", labels)
    if index is None:
        return
    if index == 0:
        cmd_restore(argparse.Namespace(model=None, dry_run=False))
        return
    item = overview[index - 1]
    model = None
    if item["models"]:
        model_index = _choose("用哪个模型", item["models"])
        if model_index is not None:
            model = item["models"][model_index]
    cmd_use(argparse.Namespace(provider=item["id"], model=model, dry_run=False))


# ---------------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-switcher",
        description="把任意 OpenAI 兼容平台接进 Codex，并随时切换模型。",
    )
    parser.add_argument("--version", action="version", version="codex-switcher " + __version__)
    parser.add_argument("--list-presets", action="store_true", help="列出内置平台预设")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化（安装凭据助手、创建目录）")
    sub.add_parser("presets", help="列出内置平台预设")

    add = sub.add_parser("add", help="添加平台")
    add.add_argument("--preset")
    add.add_argument("--id")
    add.add_argument("--name")
    add.add_argument("--base-url")
    add.add_argument("--models-url")
    add.add_argument("--transport", choices=["auto", "native", "bridge"],
                     help="连接方式：auto 自动探测（默认），native 直连 Responses，bridge 走本地协议桥")
    add.add_argument("--key", help="API Key（会出现在命令历史里，建议改用 --key-stdin）")
    add.add_argument("--key-stdin", action="store_true", help="从标准输入读取 API Key")
    add.add_argument("--model", action="append", help="手动指定模型名，可重复或用逗号分隔")
    add.add_argument("--no-discover", action="store_true", help="不自动拉取模型列表")
    add.add_argument("--no-key", action="store_true",
                     help="这个平台不需要密钥（本机模型或内网中转站）")
    add.add_argument("--console-url")
    add.add_argument("--balance-url", help="自定义余额接口地址")
    add.add_argument("--balance-path", help="余额字段路径，例如 data.balance")
    add.add_argument("--balance-currency-path", help="币种字段路径")
    add.add_argument("--balance-currency")
    add.add_argument("--balance-label")
    add.add_argument("--reasoning-effort", default="high")
    add.add_argument("--use", action="store_true", help="添加后立即切换")

    add_model = sub.add_parser("add-model", help="给某个平台手动补模型")
    context = sub.add_parser("context", help="查看 / 手改某个模型的上下文窗口")
    context.add_argument("provider")
    context.add_argument("model")
    context.add_argument("--window", type=int,
                         help="窗口 token 数，例如 131072；不填就是查看当前值")
    context.add_argument("--clear", action="store_true", help="清除手工设置，恢复按模型名推断")
    add_model.add_argument("provider")
    add_model.add_argument("models", nargs="+")

    sub.add_parser("list", help="列出已配置平台")

    models = sub.add_parser("models", help="列出某个平台的模型")
    models.add_argument("provider")

    effort = sub.add_parser("effort", help="查看 / 设置某个模型的思考强度（思考程度）")
    effort.add_argument("provider")
    effort.add_argument("--model", required=True)
    effort.add_argument("level", nargs="?", metavar="LEVEL",
                        help="none / low / medium / high / xhigh")
    effort.add_argument("--clear", action="store_true", help="清除手工档位，恢复平台默认")

    caps = sub.add_parser("capabilities", help="查看 / 实测模型能力（读图、思考、工具调用）")
    caps.add_argument("provider", nargs="?")
    caps.add_argument("--model", help="只探测一个模型")
    caps.add_argument("--probe", action="store_true", help="发真实请求实测（消耗少量 token）")
    caps.add_argument("--apply", action="store_true",
                      help="把实测结论写回目录，让 Codex 按真实能力发请求")

    use = sub.add_parser("use", help="切换默认平台与模型")
    use.add_argument("provider")
    use.add_argument("--model")
    use.add_argument("--dry-run", action="store_true")

    restore = sub.add_parser("restore", help="恢复官方 OpenAI")
    restore.add_argument("--model")
    restore.add_argument("--dry-run", action="store_true")

    balance = sub.add_parser("balance", help="查询余额 / 额度")
    balance.add_argument("provider", nargs="?")
    balance.add_argument("--no-local", action="store_true", help="不显示本机用量")

    refresh = sub.add_parser("refresh", help="重新拉取模型列表")
    refresh.add_argument("provider", nargs="?", default="all")

    bridge = sub.add_parser("bridge", help="运行本地协议桥（Chat Completions ↔ Responses）")
    bridge.add_argument("--port", type=int, default=8787)
    bridge.add_argument("--install-agent", action="store_true", help="macOS：安装为开机自启后台服务")
    bridge.add_argument("--uninstall-agent", action="store_true", help="macOS：移除后台服务")

    sub.add_parser("stop", help="停掉后台的图形界面与协议桥")

    sub.add_parser("tasks", help="列出任务，标出服务商和模型对不上的")
    repair = sub.add_parser("repair", help="修复任务的服务商绑定（换模型报 not supported）")
    repair.add_argument("--thread", help="只修一个任务（填任务 ID 前缀）")
    repair.add_argument("--dry-run", action="store_true", help="只预演，不写入")
    repair.add_argument("--deep", action="store_true",
                        help="顺带清理会话文件里残留的旧服务商（较慢，约 30 秒）")
    repair.add_argument("--follow", action="store_true",
                        help="把最近在用的任务搬到当前平台上（换平台后继续任务报 unknown model 时用）")

    history = sub.add_parser(
        "history", help="检查/清理会话历史里会让第三方平台拒绝请求的条目")
    history.add_argument("--clean", action="store_true", help="真的清理（默认只检查）")
    history.add_argument("--sweep", action="store_true",
                         help="全量清扫**所有**会话文件（不限最近多少个，推荐）")
    history.add_argument("--dry-run", action="store_true", help="配合 --clean：只预演")
    history.add_argument("--thread", help="只处理一个任务（填任务 ID 前缀）")
    history.add_argument("--scan", type=int, default=30,
                         help="检查最近多少个会话文件（默认 30）")
    history.add_argument("--all", action="store_true",
                         help="检查全部会话（很大很慢，通常不需要）")
    history.add_argument("--cross-provider", dest="cross_provider", action="store_true",
                         help="要搬到第三方平台继续：成对剥离 OpenAI 服务端工具条目")

    guard = sub.add_parser(
        "guard", help="会话体量体检：这个对话搬到目标模型上会不会陷入反复压缩")
    guard.add_argument("--model", help="目标模型（默认用当前的默认模型）")
    guard.add_argument("--provider", help="目标平台（默认自动查找声明了这个模型的平台）")
    guard.add_argument("--thread", help="指定会话文件路径（默认用最近那个）")

    quota = sub.add_parser("quota", help="记录套餐额度，用于显示本机用量百分比")
    quota.add_argument("provider")
    quota.add_argument("--tokens", type=int, help="套餐总量，例如 500000000")
    quota.add_argument("--clear", action="store_true", help="清除已记录的额度")

    update = sub.add_parser("update", help="检查是否有新版本")
    update.add_argument("--pull", action="store_true", help="确认无本地改动后自动 git pull")

    remove = sub.add_parser("remove", help="删除平台")
    remove.add_argument("provider")
    remove.add_argument("--keep-key", action="store_true")

    sub.add_parser("status", help="显示当前状态")
    check = sub.add_parser("check", help="检查「现在真的能请求当前平台吗」（定位用不了/一直重连）")
    check.add_argument("--provider", help="检查指定平台，默认检查当前正在用的")
    check.add_argument("--history", action="store_true", help="顺带检查会话历史（较慢）")

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--history", action="store_true",
                        help="顺带检查会话历史里会被第三方平台拒绝的条目（较慢）")
    doctor.add_argument("--scan", type=int, default=30,
                        help="配合 --history：检查最近多少个会话文件（默认 30）")

    app = sub.add_parser("app", help="打开图形界面")
    app.add_argument("--port", type=int, default=0)
    app.add_argument("--no-open", action="store_true")
    app.add_argument("--force-new", action="store_true",
                     help="已经有界面在运行时，仍然再开一个新的")

    export = sub.add_parser("export", help="导出脱敏状态（用于求助）")
    export.add_argument("--output")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_presets:
        return cmd_presets(args)
    handlers = {
        "init": cmd_init,
        "presets": cmd_presets,
        "add": cmd_add,
        "add-model": cmd_add_model,
        "context": cmd_context,
        "effort": cmd_effort,
        "capabilities": cmd_capabilities,
        "list": cmd_list,
        "models": cmd_models,
        "use": cmd_use,
        "restore": cmd_restore,
        "balance": cmd_balance,
        "refresh": cmd_refresh,
        "bridge": cmd_bridge,
        "stop": cmd_stop,
        "tasks": cmd_tasks,
        "repair": cmd_repair,
        "history": cmd_history,
        "guard": cmd_guard,
        "quota": cmd_quota,
        "update": cmd_update,
        "remove": cmd_remove,
        "status": cmd_status,
        "check": cmd_check,
        "doctor": cmd_doctor,
        "app": cmd_app,
        "export": cmd_export,
    }
    handler = handlers.get(getattr(args, "command", None))
    if handler is None:
        try:
            return interactive()
        except KeyboardInterrupt:
            out("")
            return 130
    try:
        return handler(args)
    except KeyboardInterrupt:
        out("")
        return 130
    except (engine.SwitchError, DiscoveryError) as exc:
        fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        fail("出错了：%s" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
