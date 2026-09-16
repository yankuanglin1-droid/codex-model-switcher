"""本地图形界面服务器。

安全设计：
  · 只监听 127.0.0.1，不对外网开放
  · 每次启动生成一次性访问令牌，URL 里必须带对才能访问
  · 不提供任何“读取密钥明文”的接口，界面里只显示脱敏串
"""

from __future__ import annotations

import json
import mimetypes
import secrets as py_secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from .. import __version__, balance as balance_module, engine, paths, registry, secrets, state as state_module, usage
from ..discovery import DiscoveryError

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _state_payload(include_balance: bool = False) -> Dict:
    providers = engine.provider_overview(include_balance=include_balance)
    local = usage.local_usage()
    for item in providers:
        stat = local.get(item["id"])
        if stat:
            item["local_usage"] = {
                "sessions": stat["sessions"],
                "turns": stat["turns"],
                "total_tokens": stat["total_tokens"],
                "total_tokens_human": usage.human_tokens(stat["total_tokens"]),
            }
    current = engine.current_status()
    return {
        "version": __version__,
        "current": current,
        "providers": providers,
        "presets": registry.preset_list(),
        "secret_backend": secrets.backend_label(),
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
            text = text.replace('href="/static/style.css"',
                                'href="/static/style.css?t=%s"' % self.token)
            text = text.replace('src="/static/app.js"',
                                'src="/static/app.js?t=%s"' % self.token)
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
            if name not in {"app.js", "style.css"}:
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
        if action == "switch":
            provider = (payload.get("provider") or "").strip()
            model = (payload.get("model") or "").strip() or None
            if provider == engine.OFFICIAL_PROVIDER:
                result = engine.switch_to(engine.OFFICIAL_PROVIDER, model)
            else:
                result = engine.switch_to(provider, model)
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


def run(port: int = 0, open_browser: bool = True, token: Optional[str] = None) -> int:
    Handler.token = token or py_secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    url = "http://127.0.0.1:%d/?t=%s" % (actual_port, Handler.token)
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
    return 0
