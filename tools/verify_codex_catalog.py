#!/usr/bin/env python3
"""端到端验证：Codex 本体能不能列出我们生成的模型。

做法是在临时 CODEX_HOME 里放一份配置，然后启动 `codex app-server`，
调用官方 `model/list` 方法，比对返回的模型清单与我们的目录是否一致。
全程不碰用户真实的 ~/.codex/config.toml。

用法：
  python3 tools/verify_codex_catalog.py
  python3 tools/verify_codex_catalog.py --codex /path/to/codex
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_switcher import catalog  # noqa: E402

CASES = [
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "native",
     ["deepseek-flash", "deepseek-v4-pro"]),
    ("minimax", "MiniMax", "https://api.minimax.cn/v1", "native",
     ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"]),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/v1", "native",
     ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-4.7"]),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "bridge",
     ["anthropic/claude-sonnet-4.5", "openai/gpt-5"]),
    ("ollama-local", "Ollama（本机）", "http://127.0.0.1:11434/v1", "bridge",
     ["qwen3:32b"]),
    ("kimi", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "bridge",
     ["kimi-k2-0905-preview", "moonshot-v1-128k"]),
]


def find_codex(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for candidate in ("codex",):
        found = shutil.which(candidate)
        if found:
            return found
    for candidate in ("~/.local/bin/codex", "/opt/homebrew/bin/codex"):
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None


def ask_model_list(codex: str, home: Path, timeout: int = 30) -> Dict:
    env = dict(os.environ, CODEX_HOME=str(home))
    process = subprocess.Popen(
        [codex, "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, bufsize=1,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout

    def send(payload):
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def receive(request_id):
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return {"error": "codex_exited"}
            for key, _ in selector.select(0.3):
                line = key.fileobj.readline()
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == request_id:
                    return message
        return {"error": "timeout"}

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "catalog-verify", "version": "1.0"},
            "capabilities": {"experimentalApi": True}}})
        if "error" in receive(1):
            return {"error": "initialize_failed"}
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "model/list", "params": {"limit": 200, "includeHidden": False}})
        response = receive(2)
        if "error" in response:
            return response
        data = response.get("result", {}).get("data", [])
        return {"ids": [item.get("id") or item.get("model") for item in data]}
    finally:
        selector.close()
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def run_case(codex: str, case) -> Dict:
    provider_id, label, base_url, transport, models = case
    with tempfile.TemporaryDirectory(prefix="codex-switcher-verify-") as temp:
        home = Path(temp)
        catalog_file = home / "catalog.json"
        document = catalog.build_catalog({"label": label}, models)
        catalog_file.write_text(json.dumps(document, ensure_ascii=False, indent=2))
        (home / "config.toml").write_text(
            'model_provider = "%s"\n'
            'model = "%s"\n'
            'model_catalog_json = "%s"\n\n'
            "[model_providers.%s]\n"
            'name = "%s"\n'
            'base_url = "%s"\n'
            'wire_api = "%s"\n\n'
            "[model_providers.%s.auth]\n"
            'command = "/bin/echo"\n'
            'args = ["placeholder"]\n'
            'timeout_ms = 3000\n'
            % (provider_id, models[0], catalog_file, provider_id, label, base_url,
               "responses", provider_id)
        )
        result = ask_model_list(codex, home)
    if "ids" not in result:
        return {"ok": False, "reason": result.get("error", "unknown")}
    returned = [item for item in result["ids"] if item]
    missing = [item for item in models if item not in returned]
    return {"ok": not missing, "returned": returned, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", help="codex 可执行文件路径")
    args = parser.parse_args()

    codex = find_codex(args.codex)
    if not codex:
        print("没有找到 codex 可执行文件，跳过此项验证。")
        return 0
    print("使用 Codex：%s" % codex)
    print("=" * 78)

    failures = 0
    for case in CASES:
        provider_id, label = case[0], case[1]
        result = run_case(codex, case)
        if result.get("ok"):
            print("✅ %-12s %-22s Codex 列表已显示 %d 个模型"
                  % (provider_id, label, len(result["returned"])))
        else:
            failures += 1
            if result.get("missing"):
                print("❌ %-12s %-22s 缺失：%s（实际返回 %s）"
                      % (provider_id, label, result["missing"], result.get("returned")))
            else:
                print("❌ %-12s %-22s 失败：%s" % (provider_id, label, result.get("reason")))
    print()
    if failures:
        print("%d 个用例未通过。" % failures)
    else:
        print("全部通过：Codex 能正确列出第三方平台的模型。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
