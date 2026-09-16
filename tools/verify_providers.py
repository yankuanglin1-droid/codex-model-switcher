#!/usr/bin/env python3
"""发布前验证：所有内置平台是否真的能接入 Codex。

检查三件事：
  1. 平台地址可达性 —— 不带密钥请求模型列表接口，看返回码
       · 200       公开可读
       · 401 / 403 接口存在，需要密钥（正常）
       · 404        地址写错了，必须修
  2. 本地链路完整性 —— 在临时 CODEX_HOME 里跑一遍「添加 → 切换 → 恢复」
  3. 有密钥的平台再做一次真实拉取（密钥只从钥匙串读，绝不打印）

用法：
  python3 tools/verify_providers.py               只做可达性与本地链路
  python3 tools/verify_providers.py --use-keychain 额外用本机钥匙串里的密钥实测
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_switcher import catalog, configfile, engine, registry, state as state_module  # noqa: E402

OK = "✅"
WARN = "⚠️ "
BAD = "❌"


def dns_hijacked(url: str) -> bool:
    """有些代理或 hosts 会把域名解析到 127.0.0.1，这种情况下连不通不是平台的问题。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        if not host or host in ("127.0.0.1", "localhost"):
            return False
        return socket.gethostbyname(host) in ("127.0.0.1", "0.0.0.0", "::1")
    except Exception:  # noqa: BLE001
        return False


def is_local(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1") or host.startswith("192.168.")
    except Exception:  # noqa: BLE001
        return False


def reachability(url: str, timeout: int = 12) -> dict:
    if not url:
        return {"status": "skip", "detail": "没有固定地址（需要用户自己填）"}
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "codex-model-switcher-verify/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4000).decode("utf-8", "replace")
            return {"status": "public", "http": response.status, "sample": body[:120]}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"status": "auth-required", "http": exc.code}
        if exc.code == 404:
            return {"status": "wrong-url", "http": exc.code}
        if exc.code == 429:
            return {"status": "rate-limited", "http": exc.code}
        if is_local(url) and exc.code >= 500:
            return {"status": "local-not-running", "detail": "本机服务返回 HTTP %d" % exc.code}
        return {"status": "http-error", "http": exc.code}
    except Exception as exc:  # noqa: BLE001
        if is_local(url):
            return {"status": "local-not-running", "detail": "本机服务未运行"}
        if dns_hijacked(url):
            return {"status": "blocked-locally", "detail": "域名在本机被解析到 127.0.0.1"}
        return {"status": "unreachable", "detail": type(exc).__name__}


def local_pipeline(preset: dict) -> dict:
    """在临时 CODEX_HOME 里跑完整流程，确认配置写入与恢复都没问题。"""
    with tempfile.TemporaryDirectory() as temp:
        os.environ["CODEX_HOME"] = temp
        home = Path(temp)
        (home / "config.toml").write_text(
            'model_provider = "openai"\nmodel = "gpt-5-codex"\n\n[desktop]\nappearanceTheme = "dark"\n'
        )
        provider_id = preset["id"] if preset["id"] != "custom" else "custom-test"
        record = engine.build_provider_record(
            provider_id=provider_id,
            label=preset["label"],
            base_url=preset["base_url"] or "https://api.example.com/v1",
            models_url=preset["models_url"] or "https://api.example.com/v1/models",
            transport=preset.get("transport", "auto") if preset.get("transport") != "auto" else "bridge",
            requires_key=False,  # 验证配置链路，不碰钥匙串
        )
        sample = list(preset["known_models"] or ["example-model"])[:3]
        record["models"] = {name: {} for name in sample}
        state_module.save({"schema_version": 3, "providers": {provider_id: record}})

        engine.switch_to(provider_id, sample[0])
        text = (home / "config.toml").read_text()
        values = configfile.read_top_level(text, ("model_provider", "model", "model_catalog_json"))
        problems = []
        if values.get("model_provider") != provider_id:
            problems.append("model_provider 未写入")
        if values.get("model") != sample[0]:
            problems.append("model 未写入")
        expected_base = engine.effective_base_url(record)
        if expected_base not in text:
            problems.append("provider 地址与预期不符")
        if 'wire_api = "responses"' not in text:
            problems.append("wire_api 不是 responses")
        catalog_path = Path(values.get("model_catalog_json") or "")
        if not catalog_path.exists():
            problems.append("模型目录未生成")
        else:
            entries = json.loads(catalog_path.read_text())["models"]
            if [item["slug"] for item in entries] != sample:
                problems.append("模型目录内容不符")
        if "[desktop]" not in text or 'appearanceTheme = "dark"' not in text:
            problems.append("无关配置被改动")
        if f"[model_providers.{provider_id}]" not in text:
            problems.append("服务商配置块未写入")

        engine.remove_provider(provider_id, purge_key=False)
        restored = configfile.read_top_level(
            (home / "config.toml").read_text(), ("model_provider", "model"))
        if restored.get("model_provider") != "openai":
            problems.append("删除后未恢复官方 provider")
        return {"ok": not problems, "problems": problems}


def keychain_probe(preset: dict) -> dict:
    """用钥匙串里的密钥真实拉一次模型列表。密钥只在本进程内使用。"""
    try:
        from codex_switcher import secrets
        key = secrets.load(preset["id"])
        if not key:
            return {"status": "no-key"}
        from codex_switcher.discovery import DiscoveryError, fetch_models
        url = preset["models_url"] or registry.derive_models_url(preset["base_url"])
        model_ids, meta = fetch_models(url, key)
        return {"status": "ok", "count": len(model_ids), "models": model_ids[:12]}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)[:160]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-keychain", action="store_true",
                        help="额外用本机钥匙串里的密钥做一次真实拉取")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    results = []
    failures = 0
    for preset in registry.PRESETS:
        reach = reachability(preset["models_url"])
        pipeline = local_pipeline(preset)
        entry = {
            "id": preset["id"],
            "label": preset["label"],
            "models_url": preset["models_url"],
            "reachability": reach,
            "pipeline": pipeline,
        }
        if args.use_keychain:
            entry["live"] = keychain_probe(preset)
        results.append(entry)
        if not pipeline["ok"] or reach["status"] in ("wrong-url", "http-error"):
            failures += 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("平台地址与本地接入验证")
        print("=" * 78)
        for entry in results:
            reach = entry["reachability"]
            pipeline = entry["pipeline"]
            if reach["status"] == "public":
                mark, note = OK, "模型接口公开可读"
            elif reach["status"] == "auth-required":
                mark, note = OK, "接口存在，需要密钥"
            elif reach["status"] == "wrong-url":
                mark, note = BAD, "地址错误（404），必须修"
            elif reach["status"] == "rate-limited":
                mark, note = WARN, "被限流，稍后再试"
            elif reach["status"] == "skip":
                mark, note = WARN, "无固定地址，由用户填写"
            elif reach["status"] == "local-not-running":
                mark, note = WARN, "本机服务未运行（装上就有）"
            elif reach["status"] == "blocked-locally":
                mark, note = WARN, "本机 DNS/代理把该域名指向了 127.0.0.1，不是平台问题"
            else:
                mark, note = WARN, "本机网络无法连通：%s" % reach.get("detail", reach.get("http"))
            pipe = OK + " 配置链路通过" if pipeline["ok"] else BAD + " " + "；".join(pipeline["problems"])
            print("%s %-12s %-24s %s" % (mark, entry["id"], entry["label"], note))
            print("   %s" % pipe)
            if entry.get("live"):
                live = entry["live"]
                if live["status"] == "ok":
                    print("   %s 真实拉取到 %d 个模型：%s" % (OK, live["count"], ", ".join(live["models"][:6])))
                elif live["status"] == "no-key":
                    print("   · 本机没有该平台密钥，跳过实测")
                else:
                    print("   %s 实测失败：%s" % (WARN, live.get("detail")))
        print()
        print("共 %d 个平台，%d 个需要处理。" % (len(results), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
