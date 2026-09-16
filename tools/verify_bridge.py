#!/usr/bin/env python3
"""端到端验证协议桥：真实 Codex → 本地桥 → 模拟「只会 Chat Completions」的平台。

这是最能说明问题的一项验证：
  · 模拟平台只实现 /chat/completions，访问 /responses 直接 404
  · 配置里让 Codex 连本地桥，wire_api = "responses"
  · 跑一次真实的 `codex exec`，看它能否拿到回答

桥通了，就说明「只支持 Chat Completions 的平台也能接进 Codex」成立。

用法：python3 tools/verify_bridge.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_switcher import bridge, catalog, state as state_module  # noqa: E402

ANSWER = "BRIDGE-OK-42"
HITS = []


def _sse(payload: dict) -> bytes:
    return ("data: %s\n\n" % json.dumps(payload)).encode("utf-8")


class MockChatOnlyUpstream(BaseHTTPRequestHandler):
    """只支持 Chat Completions 的平台。访问 /responses 会 404。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "mock-chat-model", "object": "model"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        HITS.append((self.path, self.headers.get("Authorization", "")))
        if "/responses" in self.path:
            # 真实世界里只支持 Chat 的平台就是这个反应
            self._json({"error": {"message": "unknown endpoint /responses"}}, 404)
            return
        if "/chat/completions" not in self.path:
            self._json({"error": "not found"}, 404)
            return
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for piece in ("BRIDGE-", "OK-", "42"):
                self.wfile.write(_sse({
                    "id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "mock-chat-model",
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }))
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write(_sse({
                "id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "mock-chat-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            }))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json({
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "mock-chat-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        })


def find_codex() -> str:
    for candidate in ("codex",):
        found = shutil.which(candidate)
        if found:
            return found
    for candidate in ("~/.local/bin/codex", "/opt/homebrew/bin/codex"):
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return ""


def main() -> int:
    codex = find_codex()
    if not codex:
        print("没有找到 codex 可执行文件，跳过。")
        return 0

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockChatOnlyUpstream)
    upstream_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    proxy = bridge.start_in_thread(port=0)
    bridge_port = proxy.server_address[1]

    print("Codex：%s" % codex)
    print("模拟平台（只有 chat/completions）：http://127.0.0.1:%d/v1" % upstream_port)
    print("本地协议桥：http://127.0.0.1:%d" % bridge_port)
    print("=" * 78)

    failures = []
    with tempfile.TemporaryDirectory(prefix="bridge-verify-") as temp:
        home = Path(temp)
        os.environ["CODEX_HOME"] = temp
        record = {
            "id": "bridge-demo",
            "label": "Bridge Demo",
            "base_url": "http://127.0.0.1:%d/v1" % upstream_port,
            "upstream_base_url": "http://127.0.0.1:%d/v1" % upstream_port,
            "wire_api": "responses",
            "transport": "bridge",
            "bridge_port": bridge_port,
            "requires_key": False,
            "models": {"mock-chat-model": {}},
        }
        state_module.save({"schema_version": 3, "providers": {"bridge-demo": record}})
        catalog_file = home / "catalog.json"
        catalog_file.write_text(json.dumps(
            catalog.build_catalog({"label": "Bridge Demo"}, ["mock-chat-model"]), ensure_ascii=False))
        (home / "config.toml").write_text(
            'model_provider = "bridge-demo"\n'
            'model = "mock-chat-model"\n'
            'model_catalog_json = "%s"\n'
            'model_reasoning_effort = "none"\n'
            'sandbox_mode = "read-only"\n'
            'approval_policy = "never"\n\n'
            '[model_providers.bridge-demo]\n'
            'name = "Bridge Demo"\n'
            'base_url = "http://127.0.0.1:%d/bridge-demo/v1"\n'
            'wire_api = "responses"\n' % (catalog_file, bridge_port)
        )

        command = [codex, "exec", "--skip-git-repo-check", "reply with the bridge answer"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    env=dict(os.environ, CODEX_HOME=temp), timeout=180, cwd=temp)
            output = (result.stdout or "") + (result.stderr or "")
        except subprocess.TimeoutExpired:
            output = "<timeout>"

        chat_hits = [path for path, _ in HITS if "/chat/completions" in path]
        responses_hits = [path for path, _ in HITS if "/responses" in path]
        auth_headers = [value for _, value in HITS if value]

        if ANSWER in output:
            print("✅ Codex 通过协议桥拿到了回答：%s" % ANSWER)
        else:
            failures.append("Codex 没有拿到预期回答")
            print("❌ Codex 输出里没有 %s" % ANSWER)
            print("   输出尾部：%s" % output.strip().splitlines()[-1][:200] if output.strip() else "（空）")
        if chat_hits:
            print("✅ 桥把请求翻译成了 /chat/completions（%d 次）" % len(chat_hits))
        else:
            failures.append("桥没有向上游发出 chat/completions 请求")
            print("❌ 上游没有收到 /chat/completions 请求")
        if responses_hits:
            print("   · 上游也收到了 %d 次 /responses（说明探测路径被走通了）" % len(responses_hits))
        print("   · 上游收到的 Authorization 头：%s" % ("有" if auth_headers else "无（本用例未配密钥）"))

    upstream.shutdown()
    proxy.shutdown()
    print()
    if failures:
        print("%d 项未通过。" % len(failures))
        return 1
    print("结论：只支持 Chat Completions 的平台也能接进 Codex。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
