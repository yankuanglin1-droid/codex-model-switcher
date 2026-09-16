#!/usr/bin/env python3
"""生成 docs/screenshot.png（界面截图）。

在一个临时 CODEX_HOME 里造几个演示平台，用无头 Chrome 截一张图。
**不会写入真实的钥匙串**：脚本会把凭据后端强制切成文件回退，且只写演示用的假密钥。

  python3 tools/make_screenshot.py
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_PROVIDERS = [
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "native",
     ["deepseek-flash", "deepseek-v4-pro"], "DEMO-KEY-DEEPSEEK-0001"),
    ("minimax", "MiniMax", "https://api.minimax.cn/v1", "native",
     ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5",
      "MiniMax-M2.5-highspeed", "MiniMax-M2.1", "MiniMax-M2"], "DEMO-KEY-MINIMAX-0002"),
    ("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "bridge",
     ["kimi-k2-0905-preview", "kimi-k2-turbo-preview", "moonshot-v1-128k"],
     "DEMO-KEY-MOONSHOT-0003"),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/v1", "native",
     ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo",
      "glm-4.7", "glm-4.6"], "DEMO-KEY-ZHIPU-0004"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def main() -> int:
    chrome = find_chrome()
    if not chrome:
        print("没有找到 Chrome / Chromium，跳过截图。")
        return 0

    mock_port = start_mock_balance_server()
    workdir = tempfile.mkdtemp(prefix="codex-switcher-shot-")
    os.environ["CODEX_HOME"] = workdir
    Path(workdir, "config.toml").write_text(
        'model_provider = "deepseek"\nmodel = "deepseek-flash"\n\n[desktop]\nappearanceTheme = "dark"\n')

    from codex_switcher import engine, secrets, state as state_module

    # 演示环境：强制文件回退，绝不碰真实钥匙串
    secrets._macos_available = lambda: False
    # 截图上展示 macOS 用户真实会看到的字样
    secrets.backend_label = lambda: "macOS 钥匙串"
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    state = state_module.load()
    for provider_id, label, base_url, transport, models, key in DEMO_PROVIDERS:
        record = engine.build_provider_record(provider_id=provider_id, label=label,
                                              base_url=base_url, models_url="",
                                              transport=transport)
        record["models"] = {name: {} for name in models}
        record["default_model"] = models[0]
        record["models_synced_at"] = stamp
        # 让截图里的余额卡片有内容：指向本机一个只返回演示数字的小接口。
        # 真实使用时这里接的是平台自己的余额接口（见 registry.py 的 balance 字段）。
        if provider_id == "deepseek":
            record["balance"] = {
                "kind": "json_path",
                "url": "http://127.0.0.1:%d/balance" % mock_port,
                "value_path": "data.balance",
                "currency_path": "data.currency",
                "label": "余额",
            }
            record["console_url"] = "https://platform.deepseek.com/usage"
        state_module.upsert_provider(state, record)
        secrets.store(provider_id, key)
    # 给 DeepSeek 设一个演示用的套餐额度，让“用量百分比”这张卡片有内容
    state["providers"]["deepseek"]["quota_tokens"] = 100_000_000
    state_module.save(state)
    engine.switch_to("deepseek", "deepseek-flash")

    # 造一份本机用量记录，这样进度条不是 0
    sessions = Path(workdir, "sessions", "2026", "09", "16")
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout-demo.jsonl").write_text(
        '{"type":"session_meta","payload":{"model_provider":"deepseek"}}\n'
        '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":'
        '{"input_tokens":11800000,"output_tokens":640000,"total_tokens":12440000}}}}\n',
        encoding="utf-8")

    # 预置一份“已是最新版本”的缓存，截图里就不会一直是空白
    from codex_switcher import __version__, paths as sw_paths, update as update_module
    sw_paths.ensure_dir(sw_paths.state_dir())
    sw_paths.state_dir().joinpath("update.json").write_text(json.dumps(
        {"status": "ok", "current": __version__, "latest": "v" + __version__,
         "up_to_date": True, "url": update_module.PROJECT_URL + "/releases"}, ensure_ascii=False))

    from codex_switcher.webui import server
    token = "screenshot"
    server.Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    target = ROOT / "docs" / "screenshot.png"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", "--window-size=1360,860",
                    "--virtual-time-budget=4000",
                    "--screenshot=%s" % target,
                    "http://127.0.0.1:%d/?t=%s" % (port, token)],
                   capture_output=True, timeout=120)
    httpd.shutdown()
    print("已生成：%s" % target)
    return 0


def start_mock_balance_server() -> int:
    """截图专用的假余额接口：只监听本机，只返回演示数字。"""
    import json as _json
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):  # noqa: N802
            body = _json.dumps({"data": {"balance": "42.50", "currency": "CNY"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


if __name__ == "__main__":
    raise SystemExit(main())
