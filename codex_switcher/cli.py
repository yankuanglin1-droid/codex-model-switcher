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

    if not args.clean:
        report = {"scanned": 0, "dirty": [], "totals": {"orphan_outputs": 0, "openai_only": 0}}
        for path in targets:
            report["scanned"] += 1
            info = history_module.inspect(path)
            if history_module.is_dirty(info):
                report["dirty"].append(info)
                report["totals"]["orphan_outputs"] += info["orphan_outputs"]
                report["totals"]["openai_only"] += info["openai_only"]
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
        for info in report["dirty"][:5]:
            out("    %s" % Path(info["path"]).name)
        if len(report["dirty"]) > 5:
            out("    … 还有 %d 个" % (len(report["dirty"]) - 5))
        out("")
        out("清理：codex-switcher history --clean        （会先备份）")
        out("预演：codex-switcher history --clean --dry-run")
        return 0

    official = engine.current_status().get("model_provider") == engine.OFFICIAL_PROVIDER
    report = history_module.clean(targets, moving_off_openai=not official,
                                  dry_run=args.dry_run)
    if args.dry_run:
        out("预演：会清理 %d 个会话文件" % report["changed"])
    else:
        out("已清理 %d 个会话文件" % report["changed"])
        if report["removed"]["orphan_outputs"] or report["removed"]["openai_only"]:
            out("  删掉缺 call_id 的工具结果：%d 条" % report["removed"]["orphan_outputs"])
            out("  删掉 OpenAI 专有推理条目：%d 条" % report["removed"]["openai_only"])
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
    if result["provider"] != engine.OFFICIAL_PROVIDER:
        # 旧对话的历史里可能带着只有官方 OpenAI 认识的条目，回放到第三方平台会 400。
        # 这里不主动扫（扫盘要十几秒），只把出路写清楚。
        out("提示：旧对话如果报 “missing field `call_id`”，说明它的历史里有平台不认的条目，"
            "跑 codex-switcher history --clean 就地修好。")
    state = state_module.load()
    record = state_module.get_provider(state, args.provider)
    if record and engine.resolve_transport(record) == "bridge":
        port = int(record.get("bridge_port") or 8787)
        if not engine.bridge_module.is_running(port):
            out("")
            out("⚠ 这个平台走本地协议桥，但桥还没在运行。先执行：codex-switcher bridge")
    out("注意：已经存在的旧任务仍绑定原来的平台，不会跟着切换；")
    out("     要在旧对话里继续，请对它使用「分叉」，或换个新任务。")
    return 0


def cmd_restore(args) -> int:
    model = args.model or engine.DEFAULT_OFFICIAL_MODEL
    result = engine.switch_to(engine.OFFICIAL_PROVIDER, model, dry_run=args.dry_run)
    if result.get("dry_run"):
        out("预演通过：可以恢复官方 OpenAI · %s" % result["model"])
        return 0
    out("已恢复官方 OpenAI 登录 · %s" % result["model"])
    out("配置备份：%s" % result["backup"])
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
    add_model.add_argument("provider")
    add_model.add_argument("models", nargs="+")

    sub.add_parser("list", help="列出已配置平台")

    models = sub.add_parser("models", help="列出某个平台的模型")
    models.add_argument("provider")

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

    history = sub.add_parser(
        "history", help="检查/清理会话历史里会让第三方平台拒绝请求的条目")
    history.add_argument("--clean", action="store_true", help="真的清理（默认只检查）")
    history.add_argument("--dry-run", action="store_true", help="配合 --clean：只预演")
    history.add_argument("--thread", help="只处理一个任务（填任务 ID 前缀）")
    history.add_argument("--scan", type=int, default=30,
                         help="检查最近多少个会话文件（默认 30）")
    history.add_argument("--all", action="store_true",
                         help="检查全部会话（很大很慢，通常不需要）")

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
        "quota": cmd_quota,
        "update": cmd_update,
        "remove": cmd_remove,
        "status": cmd_status,
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
