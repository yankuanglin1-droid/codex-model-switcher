"""本地图形界面服务器。

安全设计：
  · 只监听 127.0.0.1，不对外网开放
  · 每次启动生成一次性访问令牌，URL 里必须带对才能访问
  · 不提供任何“读取密钥明文”的接口，界面里只显示脱敏串
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets as py_secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from .. import (PROJECT_URL, __version__, balance as balance_module, engine, paths, registry,
                secrets, state as state_module, threads as threads_module, update as update_module,
                usage)
from .. import catalog as catalog_module
from .. import capabilities as capabilities_module
from .. import contextguard as contextguard_module
from .. import history as history_module
from .. import integrations as integrations_module
from ..discovery import DiscoveryError

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _guard_payload(current: Dict) -> Optional[Dict]:
    """给界面的上下文体量体检结果。拿不齐信息就返回 None，界面自己会不显示。"""
    try:
        model = current.get("model")
        if not model:
            return None
        catalog_path = None
        provider = current.get("model_provider")
        if provider:
            candidate = catalog_module.catalog_path(provider)
            if Path(candidate).exists():
                catalog_path = str(candidate)
        else:
            root = paths.catalog_dir()
            if root.exists():
                for item in sorted(root.glob("*.json")):
                    if contextguard_module.window_for_model(item, model):
                        catalog_path = str(item)
                        break
        candidates = contextguard_module.latest_rollouts(1)
        if not candidates:
            return None
        report = contextguard_module.assess(candidates[0], model, catalog_path=catalog_path)
    except Exception:  # noqa: BLE001 - 体检失败不能拖垮界面
        return None
    # 会话路径是隐私，只留最后一段
    report["thread"] = Path(report["path"]).name
    report.pop("path", None)
    # 光说「装不下 / 要压缩」等于把问题丢回给用户。把「这个会话还装得下谁」
    # 一并算好递过去，界面才能给出一条一键就能走完的出路。
    if report.get("action") in ("compact-first", "fork"):
        try:
            report["alternatives"] = contextguard_module.fitting_models(
                tokens=report.get("tokens") or 0)
        except Exception:  # noqa: BLE001 - 算不出备选不能拖垮界面
            report["alternatives"] = []
    return report


def _model_context(provider_id: str) -> Dict:
    """每个模型的三层上下文数字，供「选择使用模型」的地方直接看：

      · 官方窗口：模型自己支持多大（catalog 的 context_window）
      · Codex 可用：Codex 按 effective_context_window_percent（默认 95%）实际会用到的
      · 建议压缩线：Codex 自动压缩触发点（可用窗口的 60%，v1.5.5 起写进配置）

    三层都写出来，是为了让用户明白「为什么 1M 的模型在 Codex 里只有 996K 可用」。
    """
    try:
        document = json.loads(Path(catalog_module.catalog_path(provider_id)).read_text())
    except (OSError, ValueError):
        return {}
    result: Dict = {}
    for entry in document.get("models", []):
        slug = entry.get("slug")
        if not slug:
            continue
        window = 0
        try:
            window = int(entry.get("context_window") or 0)
        except (TypeError, ValueError):
            window = 0
        effective = contextguard_module.effective_window(entry) if window else 0
        compact = contextguard_module.auto_compact_limit(effective) if effective else 0
        result[slug] = {
            "window": window,
            "window_human": usage.human_tokens(window) if window else "—",
            "effective": effective,
            "effective_human": usage.human_tokens(effective) if effective else "—",
            "effective_percent": int(entry.get("effective_context_window_percent") or 95),
            "compact": compact,
            "compact_human": usage.human_tokens(compact) if compact else "—",
            "compact_ratio": int(contextguard_module.AUTO_COMPACT_RATIO * 100),
        }
    return result


def _model_windows(provider_id: str) -> Dict:
    """每个模型当前声明的窗口。界面要在卡片里显示并让人改，所以得给出来。"""
    try:
        document = json.loads(Path(catalog_module.catalog_path(provider_id)).read_text())
    except (OSError, ValueError):
        return {}
    windows: Dict = {}
    for entry in document.get("models", []):
        slug = entry.get("slug")
        if not slug:
            continue
        try:
            windows[slug] = int(entry.get("context_window") or 0)
        except (TypeError, ValueError):
            windows[slug] = 0
    return windows


def _model_efforts(provider_id: str) -> Dict:
    """每个模型当前声明的思考档位。界面要能选，所以得给出来。"""
    try:
        document = json.loads(Path(catalog_module.catalog_path(provider_id)).read_text())
    except (OSError, ValueError):
        return {}
    efforts: Dict = {}
    for entry in document.get("models", []):
        slug = entry.get("slug")
        if not slug:
            continue
        levels = [item.get("effort") for item in entry.get("supported_reasoning_levels") or []]
        efforts[slug] = {
            "levels": [item for item in levels if item],
            "current": entry.get("default_reasoning_level") or "",
        }
    return efforts


def _model_capabilities(provider_id: str, record=None) -> Dict:
    """每个模型的能力矩阵。实测过的和猜的分开标，界面上要能看出来。"""
    try:
        rows = engine.capability_matrix(provider_id)
    except Exception:  # noqa: BLE001 - 能力矩阵算不出来不能拖垮界面
        return {}
    return {row["model"]: {key: row[key] for key in
                           ("vision", "reasoning", "tools", "source", "effort", "note",
                            "context", "documented", "documented_url", "verified_at",
                            "conflict")
                           if key in row} for row in rows}


def _state_payload(include_balance: bool = False) -> Dict:
    providers = engine.provider_overview(include_balance=include_balance)
    local = usage.local_usage()
    for item in providers:
        stat = local.get(item["id"])
        used_tokens = stat["total_tokens"] if stat else 0
        if stat:
            item["local_usage"] = {
                "sessions": stat["sessions"],
                "turns": stat["turns"],
                "total_tokens": stat["total_tokens"],
                "total_tokens_human": usage.human_tokens(stat["total_tokens"]),
            }
        item["model_windows"] = _model_windows(item["id"])
        item["model_context"] = _model_context(item["id"])
        item["model_efforts"] = _model_efforts(item["id"])
        item["model_capabilities"] = _model_capabilities(item["id"])
        # 「能力查看」页要顺带告诉用户这个平台官方文档在哪、怎么接进 Codex
        item["platform_docs"] = capabilities_module.platform_docs(item["id"])
        # 同一把 API Key 在 Codex 之外还能干什么：生图 / 生视频 / 语音 /
        # 联网搜索 / 官方 MCP 与 CLI。能力页要把全量能力面摆出来。
        item["api_surface"] = capabilities_module.platform_surface(item["id"])
        # MCP / CLI 环境配好了没（add / switch 时自动配置，这里只报状态）
        item["integrations"] = integrations_module.status(item["id"])
        item["usage"] = engine.usage_with_quota(item["id"], item.get("quota_tokens"), used_tokens)
        item["usage"]["used_tokens_human"] = usage.human_tokens(used_tokens)
        if item["usage"].get("quota_tokens"):
            item["usage"]["quota_tokens_human"] = usage.human_tokens(item["usage"]["quota_tokens"])
            item["usage"]["remaining_tokens_human"] = usage.human_tokens(
                item["usage"].get("remaining_tokens", 0))
    current = engine.current_status()
    try:
        thread_info = threads_module.summarize()
    except Exception:  # noqa: BLE001 - 任务统计失败不能拖垮主界面
        thread_info = {"total": 0, "mismatch": 0, "items": []}
    return {
        # 上下文窗口守卫：界面要能一眼看出「这个对话搬到当前模型上装不装得下」。
        # 装不下又不说，用户看到的就是一直在压缩、什么都不干。
        "guard": _guard_payload(current),
        "version": __version__,
        "project_url": PROJECT_URL,
        "update": update_module.read_cache(),
        "threads": thread_info,
        "current": current,
        "providers": providers,
        "presets": registry.preset_list(),
        "secret_backend": secrets.backend_label(),
        # 同时给出机器可读的代号：界面按它翻译，避免中英混排。
        # 这里返回的中文串是给命令行用的，直接显示在英文界面上会很突兀。
        "secret_backend_id": secrets.backend(),
        "codex_missing": not current.get("exists", False),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexModelSwitcher/" + __version__
    token = ""

    # ---------------------------------------------------------- 基础工具
    def log_message(self, format, *args):  # noqa: A002 - 保持签名
        return  # 静默，避免把访问细节写到终端

    def _authorized(self, query: Dict) -> bool:
        supplied = (query.get("t") or [""])[0]
        return bool(supplied) and py_secrets.compare_digest(supplied, self.token)

    def _send_json(self, payload: Dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404, "not found")
            return
        body = path.read_bytes()
        # 页面里的静态资源也要带上令牌，否则样式和脚本会被自己拦下来
        if path.name == "index.html":
            text = body.decode("utf-8")
            # 用正则一次覆盖所有 /static/ 引用，而不是把文件名一个个写死。
            # 写死过一次：后来加了 i18n.js，忘了往这里补，结果脚本取不到令牌
            # 被 403 挡掉，界面直接报 "toggleLang is not defined"。
            text = re.sub(
                r'((?:src|href)="/static/[^"?]+)"',
                lambda m: '%s?t=%s"' % (m.group(1), self.token),
                text)
            body = text.encode("utf-8")
        mime, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", (mime or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 2 * 1024 * 1024))
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # -------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not self._authorized(query):
                self.send_error(403, "invalid token")
                return
            self._send_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            if not self._authorized(query):
                self.send_error(403, "invalid token")
                return
            name = Path(path).name
            if name not in {"app.js", "i18n.js", "style.css", "logo.svg"}:
                self.send_error(404, "not found")
                return
            self._send_file(STATIC_DIR / name)
            return
        if path == "/api/state":
            if not self._authorized(query):
                self._send_json({"error": "unauthorized"}, 403)
                return
            include_balance = (query.get("balance") or ["0"])[0] == "1"
            self._send_json(_state_payload(include_balance=include_balance))
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if not self._authorized(query):
            self._send_json({"error": "unauthorized"}, 403)
            return
        payload = self._read_json()
        action = parsed.path.rsplit("/", 1)[-1]
        try:
            result = self._dispatch(action, payload)
        except engine.SwitchError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except DiscoveryError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            return
        self._send_json(result)

    def _dispatch(self, action: str, payload: Dict) -> Dict:
        if action == "fit_switch":
            # 上下文守卫的自动化出口：换到一个装得下当前会话的模型。
            # switch_to 会顺带把最近在用的任务搬过去并重写压缩触发点。
            provider = (payload.get("provider") or "").strip()
            model = (payload.get("model") or "").strip()
            if not provider or not model:
                return {"error": "缺少平台或模型"}
            try:
                result = engine.switch_to(provider, model)
            except engine.SwitchError as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "switch":
            provider = (payload.get("provider") or "").strip()
            model = (payload.get("model") or "").strip() or None
            if provider == engine.OFFICIAL_PROVIDER:
                result = engine.switch_to(engine.OFFICIAL_PROVIDER, model)
            else:
                result = engine.switch_to(provider, model)
            result["state"] = _state_payload()
            return result

        if action == "set_context":
            provider_id = (payload.get("provider") or "").strip()
            model_id = (payload.get("model") or "").strip()
            window = payload.get("window")
            if not provider_id or not model_id:
                return {"error": "缺少平台或模型"}
            try:
                result = engine.set_context_window(
                    provider_id, model_id, int(window) if window else None)
            except engine.SwitchError as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "set_effort":
            provider_id = (payload.get("provider") or "").strip()
            model_id = (payload.get("model") or "").strip()
            effort = (payload.get("effort") or "").strip()
            if not provider_id or not model_id:
                return {"error": "缺少平台或模型"}
            try:
                result = engine.set_reasoning_effort(provider_id, model_id, effort or None)
            except engine.SwitchError as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "set_capability":
            provider_id = (payload.get("provider") or "").strip()
            model_id = (payload.get("model") or "").strip()
            key = (payload.get("key") or "vision").strip()
            if not provider_id or not model_id:
                return {"error": "缺少平台或模型"}
            value = payload.get("value")
            if value is None and "vision" in payload:
                value = "yes" if payload.get("vision") else "no"
            try:
                result = engine.set_model_capability(
                    provider_id, model_id, key=key,
                    value=None if value is None else str(value))
            except (engine.SwitchError, ValueError) as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "restart_codex":
            # Codex 只在启动时读一次配置：切完模型点确认，由这里代劳重启
            from .. import appctl
            return appctl.restart_codex()

        if action == "sync_integrations":
            provider_id = (payload.get("provider") or "").strip()
            if not provider_id:
                return {"error": "缺少平台"}
            try:
                result = engine.sync_integrations(provider_id)
            except engine.SwitchError as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "probe_capabilities":
            provider_id = (payload.get("provider") or "").strip()
            model_id = (payload.get("model") or "").strip() or None
            if not provider_id:
                return {"error": "缺少平台"}
            try:
                result = engine.probe_capabilities(
                    provider_id, model_id, apply_result=bool(payload.get("apply")))
            except engine.SwitchError as exc:
                return {"error": str(exc)}
            result["state"] = _state_payload()
            return result

        if action == "restore":
            result = engine.switch_to(engine.OFFICIAL_PROVIDER, payload.get("model"))
            result["state"] = _state_payload()
            return result

        if action == "refresh":
            provider_id = (payload.get("provider") or "").strip()
            if provider_id in ("", "all"):
                targets = state_module.provider_ids(state_module.load())
                updated, errors = 0, []
                for target in targets:
                    try:
                        engine.refresh_models(target)
                        updated += 1
                    except Exception as exc:  # noqa: BLE001 - 单个平台失败不影响其它平台
                        errors.append("%s：%s" % (target, exc))
                return {"models": [], "updated": updated, "errors": errors, "state": _state_payload()}
            result = engine.refresh_models(provider_id)
            return {"models": result["models"], "state": _state_payload()}

        if action == "balance":
            state = state_module.load()
            provider_id = (payload.get("provider") or "").strip()
            record = state_module.get_provider(state, provider_id)
            if not record:
                raise engine.SwitchError("没有找到平台：%s" % provider_id)
            result = balance_module.query(record, secrets.load(provider_id))
            stat = usage.local_usage().get(provider_id)
            if stat:
                result["local_usage"] = {
                    "sessions": stat["sessions"],
                    "total_tokens_human": usage.human_tokens(stat["total_tokens"]),
                }
            return result

        if action == "add":
            return self._add(payload)

        if action == "add-model":
            state = state_module.load()
            record = state_module.get_provider(state, (payload.get("provider") or "").strip())
            if not record:
                raise engine.SwitchError("没有找到平台")
            from .. import catalog as catalog_module
            models = record.get("models") or {}
            for name in payload.get("models") or []:
                name = (name or "").strip()
                if name:
                    models[name] = {"manual": True}
            record["models"] = models
            state_module.save(state)
            catalog_module.write_catalog(record["id"], record, list(models.keys()))
            return {"models": list(models.keys()), "state": _state_payload()}

        if action == "remove":
            result = engine.remove_provider((payload.get("provider") or "").strip(),
                                            purge_key=bool(payload.get("purge_key", True)))
            result["state"] = _state_payload()
            return result

        if action == "quota":
            provider_id = (payload.get("provider") or "").strip()
            tokens = payload.get("tokens")
            engine.set_quota(provider_id, None if payload.get("clear") else int(tokens or 0))
            return {"state": _state_payload()}

        if action == "update-check":
            result = update_module.check(force=True)
            result["description"] = update_module.describe(result)
            return result

        if action == "threads":
            return threads_module.summarize()

        if action == "repair":
            thread_id = (payload.get("thread") or "").strip() or None
            report = threads_module.repair(thread_id=thread_id,
                                           dry_run=bool(payload.get("dry_run")))
            report["description"] = threads_module.describe(report)
            report["state"] = _state_payload()
            return report

        if action == "sweep_history":
            # 全量清扫会话历史里的孤儿工具结果（缺 call_id 的 function_call_output）。
            # 有预算上限：几百 MB 的大文件不该把界面卡住，剩下的交给后台巡检。
            report = history_module.sweep_all(
                dry_run=bool(payload.get("dry_run")),
                budget_seconds=SWEEP_ON_DEMAND_BUDGET_SECONDS)
            report["description"] = history_module.describe_sweep(report)
            return report

        raise engine.SwitchError("未知操作：%s" % action)

    def _add(self, payload: Dict) -> Dict:
        preset_id = (payload.get("preset_id") or "").strip() or None
        preset = registry.preset(preset_id) if preset_id else None

        label = (payload.get("label") or (preset or {}).get("label") or "").strip()
        base_url = (payload.get("base_url") or (preset or {}).get("base_url") or "").strip()
        if not base_url:
            raise engine.SwitchError("请填写 Base URL")
        if not label:
            label = base_url

        models_url = (payload.get("models_url") or (preset or {}).get("models_url") or "").strip()
        transport = (payload.get("transport") or (preset or {}).get("transport") or "auto").strip()
        if transport not in ("auto", "native", "bridge"):
            transport = "auto"
        api_key = (payload.get("key") or "").strip() or None
        requires_key = bool(payload.get("requires_key", (preset or {}).get("requires_key", True)))

        manual_models = payload.get("models") or []
        auto_discover = bool(payload.get("auto_discover", True))

        balance_spec = (preset or {}).get("balance")
        balance_url = (payload.get("balance_url") or "").strip()
        if balance_url:
            balance_spec = {
                "kind": "json_path",
                "url": balance_url,
                "value_path": (payload.get("balance_path") or "").strip(),
                "currency_path": (payload.get("balance_currency_path") or "").strip(),
                "currency": (payload.get("balance_currency") or "").strip(),
                "label": (payload.get("balance_label") or "余额").strip(),
            }

        result = engine.add_provider(
            provider_id=(payload.get("id") or "").strip() or (preset or {}).get("id"),
            label=label,
            base_url=base_url,
            models_url=models_url,
            transport=transport,
            api_key=api_key,
            preset_id=preset_id,
            balance=balance_spec,
            console_url=(payload.get("console_url") or (preset or {}).get("console_url")),
            requires_key=requires_key,
            notes=(preset or {}).get("notes"),
            manual_models=manual_models,
            auto_discover=auto_discover,
        )
        payload_out = {
            "provider": result["record"]["id"],
            "models": result["models"],
            "discovery_error": result["discovery_error"],
            "state": _state_payload(),
        }
        return payload_out


WATCHDOG_INTERVAL_SECONDS = 12.0
# 全量历史清扫比绑定修复重得多（首次要把所有会话文件读一遍，本机 33GB），
# 所以隔几拍跑一次，每次只给一小段预算 —— 分多轮收敛，不跟界面抢磁盘。
SWEEP_EVERY_N_TICKS = 5
WATCHDOG_SWEEP_BUDGET_SECONDS = 8.0
SWEEP_ON_DEMAND_BUDGET_SECONDS = 12.0


def _log_sweep(report: Dict) -> None:
    target = paths.state_dir() / "history-sweep.log"
    try:
        paths.ensure_dir(target.parent)
        with target.open("a", encoding="utf-8") as stream:
            stream.write("[%s] %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S"),
                history_module.describe_sweep(report).replace("\n", " ")))
    except OSError:
        pass


def _watchdog_loop(interval: float = WATCHDOG_INTERVAL_SECONDS) -> None:
    """后台巡检：修任务绑定 + 清会话历史里的坏条目。

    为什么需要它：minimax / deepseek / glm 都是 transport=native，直连平台
    API，根本不过本地协议桥，所以桥上那个巡检线程管不到它们。而「切换后
    继续任务」产生的错位，往往是在 Codex 把新模型写回数据库**之后**才出现
    的 —— 只在切换那一刻修一次根本来不及。这里定期复查，几秒内自动纠偏。

    历史清扫也放这儿，因为 `codex_app` 命名空间的工具（automation_update
    等）**会持续**写出缺 call_id 的孤儿工具结果：定时任务每跑一次就可能
    多一条。一次性清完不够用，得有人一直盯着。账本保证每轮只读变过的文件。
    """
    tick = 0
    while True:
        try:
            time.sleep(interval)
        except Exception:  # noqa: BLE001 - 退出路径，不必细分
            return
        tick += 1
        try:
            report = threads_module.repair()
            if report.get("fixed"):
                threads_module.log_watch(report)
        except Exception:  # noqa: BLE001 - 巡检绝不能把界面拖垮
            pass
        if tick % SWEEP_EVERY_N_TICKS == 0:
            try:
                sweep = history_module.sweep_all(
                    budget_seconds=WATCHDOG_SWEEP_BUDGET_SECONDS)
                if sweep.get("cleaned"):
                    _log_sweep(sweep)
            except Exception:  # noqa: BLE001 - 同上
                pass


def _start_watchdog(interval: float = WATCHDOG_INTERVAL_SECONDS) -> None:
    worker = threading.Thread(target=_watchdog_loop, args=(interval,), daemon=True)
    worker.start()


def run(port: int = 0, open_browser: bool = True, token: Optional[str] = None) -> int:
    Handler.token = token or py_secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    url = "http://127.0.0.1:%d/?t=%s" % (actual_port, Handler.token)
    _write_runtime_state(actual_port)
    # 开界面先自查一遍，再交给后台巡检兜着
    try:
        threads_module.repair()
    except Exception:  # noqa: BLE001
        pass
    _start_watchdog()
    # 开界面顺手把存量坏条目清一遍：孤儿工具结果不挑新旧，散落在任意老会话
    # 文件里，用户点开哪个就炸哪个。后台跑，不挡界面。
    try:
        engine.schedule_history_sweep()
    except Exception:  # noqa: BLE001
        pass
    print("图形界面已启动：%s" % url)
    print("（只监听本机，关闭此终端窗口即停止）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
        _clear_runtime_state()
    return 0


def _write_runtime_state(port: int) -> None:
    """记下端口和进程号，方便 `codex-switcher stop` 关掉它。"""
    try:
        import os
        paths.ensure_dir(paths.state_dir())
        paths.gui_state_file().write_text(json.dumps(
            {"port": port, "pid": os.getpid(), "token": Handler.token},
            ensure_ascii=False, indent=2) + "\n")
        os.chmod(paths.gui_state_file(), 0o600)
    except OSError:
        pass


def _clear_runtime_state() -> None:
    try:
        paths.gui_state_file().unlink()
    except OSError:
        pass


def existing_url(timeout: float = 1.0) -> Optional[str]:
    """如果已经有一个活着的图形界面，返回它的访问地址（供 .app 重复点击时复用）。"""
    import socket
    try:
        record = json.loads(paths.gui_state_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    port = record.get("port")
    token = record.get("token")
    if not isinstance(port, int) or not token:
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except OSError:
        return None
    return "http://127.0.0.1:%d/?t=%s" % (port, token)
