"""核心逻辑测试。不联网、不碰真实配置：全部跑在临时 CODEX_HOME 里。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_switcher import configfile as configfile_module  # noqa: E402

TOML_OK = configfile_module.toml_available()


def load_document(text):
    """需要完整 TOML 解析的断言用这个；没有解析库时整条测试标记为跳过。"""
    if not TOML_OK:
        raise unittest.SkipTest("当前 Python 没有 tomllib/tomli")
    return configfile_module.parse(text)


SAMPLE_CONFIG = '''personality = "pragmatic"
sandbox_mode = "danger-full-access"

model_provider = "openai"
model = "gpt-5-codex"
model_reasoning_effort = "medium"

[desktop]
appearanceTheme = "dark"
enabled-reasoning-efforts = ["low", "medium", "high"]

[mcp_servers.example]
command = "/usr/bin/true"

[model_providers.existing]
name = "Existing"
base_url = "https://example.com/v1"
wire_api = "chat"
'''


class TempCodexHome(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self._temp.name
        self.home = Path(self._temp.name)
        (self.home / "config.toml").write_text(SAMPLE_CONFIG)

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old_home
        self._temp.cleanup()


class ConfigFileTests(TempCodexHome):
    def test_rewrite_keeps_everything_else(self):
        from codex_switcher import configfile
        text = (self.home / "config.toml").read_text()
        new = configfile.rewrite_model_settings(text, {
            "model_provider": "demo",
            "model": "demo-model",
            "model_reasoning_effort": "high",
            "model_catalog_json": "/tmp/demo.json",
        })
        before = load_document(text)
        after = load_document(new)
        for key in ("desktop", "mcp_servers", "model_providers"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["model_provider"], "demo")
        self.assertEqual(after["model"], "demo-model")
        self.assertEqual(after["model_reasoning_effort"], "high")
        self.assertNotIn("service_tier", after)

    def test_rewrite_is_idempotent(self):
        from codex_switcher import configfile
        text = (self.home / "config.toml").read_text()
        settings = {"model_provider": "demo", "model": "m"}
        once = configfile.rewrite_model_settings(text, settings)
        twice = configfile.rewrite_model_settings(once, settings)
        self.assertEqual(once, twice)

    def test_upsert_provider_block_adds_and_replaces(self):
        from codex_switcher import configfile
        text = (self.home / "config.toml").read_text()
        fields = {"name": "Demo", "base_url": "https://api.demo.com/v1", "wire_api": "chat"}
        auth = {"command": "/tmp/helper.py", "args": ["demo"]}
        added = configfile.upsert_provider_block(text, "demo", fields, auth)
        parsed = load_document(added)
        self.assertEqual(parsed["model_providers"]["demo"]["base_url"], "https://api.demo.com/v1")
        self.assertEqual(parsed["model_providers"]["demo"]["auth"]["args"], ["demo"])
        # 再写一次不同地址，应该替换而不是重复
        fields2 = dict(fields, base_url="https://api.demo.com/v2")
        replaced = configfile.upsert_provider_block(added, "demo", fields2, auth)
        parsed2 = load_document(replaced)
        self.assertEqual(parsed2["model_providers"]["demo"]["base_url"], "https://api.demo.com/v2")
        self.assertEqual(replaced.count("[model_providers.demo]"), 1)

    def test_upsert_runs_on_a_real_messy_config(self):
        from codex_switcher import configfile
        messy = SAMPLE_CONFIG + '''
[projects."/tmp/x"]
trust_level = "trusted"

[model_providers.demo]
name = "Old"
base_url = "https://old.example.com"
wire_api = "chat"

[model_providers.demo.auth]
command = "/tmp/old.py"
args = ["demo"]
'''
        fields = {"name": "New", "base_url": "https://new.example.com/v1", "wire_api": "responses"}
        auth = {"command": "/tmp/new.py", "args": ["demo"]}
        updated = configfile.upsert_provider_block(messy, "demo", fields, auth)
        parsed = load_document(updated)
        self.assertEqual(parsed["model_providers"]["demo"]["name"], "New")
        self.assertEqual(parsed["model_providers"]["demo"]["wire_api"], "responses")
        self.assertEqual(parsed["projects"]["/tmp/x"]["trust_level"], "trusted")
        self.assertNotIn("old.example.com", updated)

    def test_remove_provider_block(self):
        from codex_switcher import configfile
        text = SAMPLE_CONFIG + '''
[model_providers.gone]
name = "Gone"
base_url = "https://gone.example.com"
wire_api = "chat"
'''
        trimmed = configfile.remove_provider_block(text, "gone")
        parsed = load_document(trimmed)
        self.assertNotIn("gone", parsed.get("model_providers", {}))
        self.assertIn("existing", parsed["model_providers"])

    def test_multiline_strings_do_not_confuse_scanner(self):
        from codex_switcher import configfile
        tricky = '''model = "m"

[model_providers.x]
name = "X"
base_url = "https://x.example.com"
wire_api = "chat"
instruction = """
[model_providers.fake]
this is not a real table
"""
'''
        blocks = configfile._table_blocks(tricky.splitlines(keepends=True))
        names = {name for name, _, _ in blocks}
        self.assertIn("model_providers.x", names)
        self.assertNotIn("model_providers.fake", names)


class CatalogTests(unittest.TestCase):
    def test_build_catalog_shape(self):
        from codex_switcher import catalog
        provider = {"label": "Demo", "model_overrides": {}}
        document = catalog.build_catalog(provider, ["MiniMax-M3", "deepseek-flash"])
        self.assertEqual(len(document["models"]), 2)
        entry = document["models"][0]
        for key in ("slug", "display_name", "default_reasoning_level", "supported_reasoning_levels",
                    "shell_type", "visibility", "supported_in_api", "truncation_policy",
                    "input_modalities", "context_window", "effective_context_window_percent"):
            self.assertIn(key, entry)
        self.assertIn("image", document["models"][0]["input_modalities"])
        self.assertNotIn("image", document["models"][1]["input_modalities"])

    def test_priority_follows_order(self):
        from codex_switcher import catalog
        document = catalog.build_catalog({"label": "D"}, ["a", "b", "c"])
        self.assertEqual([item["priority"] for item in document["models"]], [0, 1, 2])


class RegistryTests(unittest.TestCase):
    def test_presets_are_unique(self):
        from codex_switcher import registry
        ids = [item["id"] for item in registry.PRESETS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_preset_has_required_fields(self):
        from codex_switcher import registry
        for item in registry.PRESETS:
            for key in ("id", "label", "transport", "balance"):
                self.assertIn(key, item, item["id"])
            self.assertIn(item["transport"], ("native", "auto", "bridge"))
            if item["id"] != "custom":
                self.assertTrue(item["base_url"].startswith("http"), item["id"])

    def test_no_preset_uses_a_reserved_provider_id(self):
        from codex_switcher import registry
        for item in registry.PRESETS:
            self.assertNotIn(item["id"], registry.RESERVED_PROVIDER_IDS, item["id"])

    def test_wire_api_is_always_responses(self):
        # Codex 已不接受 chat，写成 chat 会直接拒绝启动
        from codex_switcher import registry
        self.assertEqual(registry.WIRE_API, "responses")

    def test_derive_models_url(self):
        from codex_switcher import registry
        self.assertEqual(registry.derive_models_url("https://a.com/v1"), "https://a.com/v1/models")
        self.assertEqual(registry.derive_models_url("https://a.com"), "https://a.com/v1/models")
        self.assertEqual(registry.derive_models_url("https://a.com/v1/"), "https://a.com/v1/models")

    def test_hint_lookup(self):
        from codex_switcher import registry
        self.assertIn("image", registry.hint_for("MiniMax-M3")["modalities"])
        self.assertEqual(registry.hint_for("totally-unknown-model")["context"], 131072)


class BalanceTests(unittest.TestCase):
    def test_deepseek_adapter_parsing(self):
        from codex_switcher import balance
        original = balance._http_json
        balance._http_json = lambda *a, **k: {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "16.04",
                               "granted_balance": "0.00", "topped_up_balance": "16.04"}],
        }
        try:
            result = balance.query({"id": "deepseek", "balance": {"kind": "builtin", "adapter": "deepseek"},
                                    "base_url": "https://api.deepseek.com"}, "fake-key")
        finally:
            balance._http_json = original
        self.assertEqual(result["status"], "ok")
        self.assertIn("16.04", result["display"])

    def test_unsupported_platform_is_honest(self):
        from codex_switcher import balance
        result = balance.query({"id": "x", "balance": None, "console_url": "https://x"}, "k")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("未开放", result["display"])

    def test_json_path_adapter(self):
        from codex_switcher import balance
        original = balance._http_json
        balance._http_json = lambda *a, **k: {"data": {"wallet": {"balance": 12.5, "currency": "USD"}}}
        try:
            result = balance.query({
                "id": "custom",
                "balance": {"kind": "json_path", "url": "https://x/balance",
                            "value_path": "data.wallet.balance", "currency_path": "data.wallet.currency"},
            }, "k")
        finally:
            balance._http_json = original
        self.assertEqual(result["status"], "ok")
        self.assertIn("12.5", result["display"])
        self.assertIn("USD", result["display"])

    def test_missing_path_reports_error(self):
        from codex_switcher import balance
        original = balance._http_json
        balance._http_json = lambda *a, **k: {"data": {}}
        try:
            result = balance.query({
                "id": "custom",
                "balance": {"kind": "json_path", "url": "https://x", "value_path": "data.nope"},
            }, "k")
        finally:
            balance._http_json = original
        self.assertEqual(result["status"], "error")


class SecretsTests(unittest.TestCase):
    def test_mask_never_leaks_whole_key(self):
        from codex_switcher import secrets
        # 故意用一个不像真密钥的示例串，避免被发布前的密钥扫描误报
        key = "EXAMPLE-NOT-A-REAL-KEY"
        masked = secrets.mask(key)
        self.assertNotIn(key, masked)
        self.assertIn("…", masked)
        self.assertEqual(secrets.mask(None), "未配置")


class EngineTests(TempCodexHome):
    def test_slugify(self):
        from codex_switcher import engine
        self.assertEqual(engine.slugify("My Provider!"), "my-provider")
        self.assertEqual(engine.slugify("中文平台"), "provider")
        self.assertTrue(engine.slugify("1abc").startswith("p-"))

    def test_switch_writes_config_and_catalog(self):
        from codex_switcher import configfile, engine, paths
        record = engine.build_provider_record(
            provider_id="local-ollama",
            label="本地模型",
            base_url="http://127.0.0.1:11434/v1",
            models_url="http://127.0.0.1:11434/v1/models",
            transport="native",
            requires_key=False,
        )
        record["models"] = {"qwen3:32b": {}}
        from codex_switcher import state as state_module
        state_module.save({"schema_version": 3, "providers": {"local-ollama": record}})

        result = engine.switch_to("local-ollama", "qwen3:32b")
        self.assertEqual(result["model"], "qwen3:32b")

        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["model_provider"], "local-ollama")
        self.assertEqual(parsed["model"], "qwen3:32b")
        self.assertEqual(parsed["model_providers"]["local-ollama"]["base_url"],
                         "http://127.0.0.1:11434/v1")
        self.assertEqual(parsed["model_providers"]["local-ollama"]["wire_api"], "responses")
        self.assertNotIn("auth", parsed["model_providers"]["local-ollama"])
        self.assertEqual(parsed["desktop"]["appearanceTheme"], "dark")
        catalog_path = Path(parsed["model_catalog_json"])
        self.assertTrue(catalog_path.exists())
        self.assertEqual(json.loads(catalog_path.read_text())["models"][0]["slug"], "qwen3:32b")

    def test_bridge_transport_points_codex_at_local_bridge(self):
        from codex_switcher import engine, paths, state as state_module
        record = engine.build_provider_record(
            provider_id="kimi",
            label="Kimi",
            base_url="https://api.moonshot.cn/v1",
            models_url="https://api.moonshot.cn/v1/models",
            transport="bridge",
            requires_key=False,
        )
        record["models"] = {"kimi-k2": {}}
        state_module.save({"schema_version": 3, "providers": {"kimi": record}})
        engine.switch_to("kimi", "kimi-k2")
        block = load_document(paths.config_path().read_text())["model_providers"]["kimi"]
        # Codex 连本地桥，真实平台地址保留在状态文件里
        self.assertTrue(block["base_url"].startswith("http://127.0.0.1:"))
        self.assertIn("/kimi/v1", block["base_url"])
        self.assertEqual(block["wire_api"], "responses")
        self.assertEqual(record["upstream_base_url"], "https://api.moonshot.cn/v1")

    def test_reserved_ids_are_renamed(self):
        from codex_switcher import engine, state as state_module
        state_module.save({"schema_version": 3, "providers": {}})
        state = state_module.load()
        self.assertEqual(engine.unique_id(state, "ollama"), "ollama-local")
        self.assertEqual(engine.unique_id(state, "openai"), "openai-local")
        self.assertEqual(engine.unique_id(state, "deepseek"), "deepseek")

    def test_switch_rejects_unknown_model(self):
        from codex_switcher import engine, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="https://api.demo.com/v1/models", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})
        with self.assertRaises(engine.SwitchError):
            engine.switch_to("demo", "not-there")

    def test_backup_written_before_change(self):
        from codex_switcher import engine, paths, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="https://api.demo.com/v1/models", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})
        engine.switch_to("demo", "a")
        backups = list(paths.backups_dir().glob("*.toml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), SAMPLE_CONFIG)

    def test_restore_official(self):
        from codex_switcher import configfile, engine, paths, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="https://api.demo.com/v1/models", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})
        engine.switch_to("demo", "a")
        engine.switch_to(engine.OFFICIAL_PROVIDER, "gpt-5-codex")
        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["model"], "gpt-5-codex")
        self.assertNotIn("model_catalog_json", parsed)
        # 平台配置仍保留，随时可以切回去
        self.assertIn("demo", parsed["model_providers"])

    def test_remove_provider_purges_block_and_record(self):
        from codex_switcher import configfile, engine, paths, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="https://api.demo.com/v1/models", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})
        engine.switch_to("demo", "a")
        engine.remove_provider("demo")
        parsed = load_document(paths.config_path().read_text())
        self.assertNotIn("demo", parsed.get("model_providers", {}))
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(state_module.load()["providers"], {})


class DiscoveryTests(unittest.TestCase):
    def test_extract_ids_shapes(self):
        from codex_switcher import discovery
        self.assertEqual(discovery._extract_ids({"data": [{"id": "a"}, {"id": "b"}]}), ["a", "b"])
        self.assertEqual(discovery._extract_ids({"models": [{"name": "c"}]}), ["c"])
        self.assertEqual(discovery._extract_ids([{"model": "d"}, "e"]), ["d", "e"])
        self.assertEqual(discovery._extract_ids({"nope": 1}), [])

    def test_rank_prefers_newer_and_pro(self):
        from codex_switcher import discovery
        ranked = discovery.rank_models(["glm-4.5", "glm-4.7", "glm-4.6", "MiniMax-M3", "MiniMax-M2.5"])
        self.assertEqual(ranked[0], "glm-4.7")
        self.assertLess(ranked.index("glm-4.6"), ranked.index("glm-4.5"))

    def test_probe_responses_maps_status_codes(self):
        import urllib.error
        from codex_switcher import discovery

        class FakeOpener:
            def __init__(self, code):
                self.code = code

            def __call__(self, request, timeout=None):
                if self.code is None:
                    raise RuntimeError("boom")
                raise urllib.error.HTTPError(request.full_url, self.code, "x", {}, None)

        original = discovery.urllib.request.urlopen
        try:
            discovery.urllib.request.urlopen = FakeOpener(404)
            self.assertEqual(discovery.probe_responses("https://a.com/v1")["transport"], "bridge")
            discovery.urllib.request.urlopen = FakeOpener(429)
            self.assertEqual(discovery.probe_responses("https://a.com/v1")["transport"], "native")
            discovery.urllib.request.urlopen = FakeOpener(401)
            self.assertEqual(discovery.probe_responses("https://a.com/v1")["transport"], "unknown")
            discovery.urllib.request.urlopen = FakeOpener(None)
            self.assertEqual(discovery.probe_responses("https://a.com/v1")["transport"], "unknown")
        finally:
            discovery.urllib.request.urlopen = original


class BridgeTests(unittest.TestCase):
    """协议桥的请求/响应翻译。"""

    def test_responses_request_becomes_chat_request(self):
        from codex_switcher import bridge
        payload = bridge.responses_to_chat({
            "model": "m1",
            "instructions": "be nice",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hello"}]},
                {"type": "function_call", "call_id": "c1", "name": "read_file",
                 "arguments": "{\"path\":\"a\"}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
            "tools": [{"type": "function", "name": "read_file",
                       "description": "read", "parameters": {"type": "object"}}],
            "max_output_tokens": 128,
            "stream": False,
        })
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "be nice"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(payload["messages"][3]["role"], "tool")
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_file")
        self.assertEqual(payload["max_tokens"], 128)
        self.assertFalse(payload["stream"])

    def test_chat_response_becomes_responses_output(self):
        from codex_switcher import bridge
        document = bridge.chat_to_responses({
            "id": "c1", "model": "m1",
            "choices": [{"message": {"role": "assistant", "content": "hi there"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }, "m1")
        self.assertEqual(document["object"], "response")
        self.assertEqual(document["status"], "completed")
        item = document["output"][0]
        self.assertEqual(item["type"], "message")
        self.assertEqual(item["content"][0]["text"], "hi there")
        self.assertEqual(document["usage"]["total_tokens"], 5)

    def test_tool_call_response_becomes_function_call_item(self):
        from codex_switcher import bridge
        document = bridge.chat_to_responses({
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "call_9", "type": "function",
                                                     "function": {"name": "ls", "arguments": "{}"}}]}}],
        }, "m1")
        self.assertEqual(document["output"][0]["type"], "function_call")
        self.assertEqual(document["output"][0]["call_id"], "call_9")

    def test_stream_state_emits_expected_event_order(self):
        from codex_switcher import bridge
        state = bridge._StreamState("m1")
        events = [block.decode() for block in state.preamble()]
        events += [block.decode() for block in state.delta({"choices": [{"delta": {"content": "he"}}]})]
        events += [block.decode() for block in state.delta({"choices": [{"delta": {"content": "llo"}}]})]
        events += [block.decode() for block in state.finish({"total_tokens": 7})]
        names = [line[len("event: "):] for text in events for line in text.splitlines()
                 if line.startswith("event: ")]
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names[-1], "response.completed")
        self.assertEqual(names.count("response.output_text.delta"), 2)


class FallbackTests(TempCodexHome):
    """没有 TOML 库（例如系统自带 Python 3.9）时也必须能安全改写配置。"""

    def setUp(self) -> None:
        super().setUp()
        self._saved = configfile_module.tomllib
        configfile_module.tomllib = None

    def tearDown(self) -> None:
        configfile_module.tomllib = self._saved
        super().tearDown()

    def test_toml_available_reports_false(self):
        self.assertFalse(configfile_module.toml_available())

    def test_rewrite_still_works_without_toml_library(self):
        text = (self.home / "config.toml").read_text()
        new = configfile_module.rewrite_model_settings(text, {
            "model_provider": "demo",
            "model": "demo-model",
        })
        self.assertIn('model_provider = "demo"', new)
        self.assertIn('model = "demo-model"', new)
        self.assertIn("[desktop]", new)
        values = configfile_module.read_top_level(new, ("model_provider", "model"))
        self.assertEqual(values, {"model_provider": "demo", "model": "demo-model"})

    def test_provider_block_still_written_without_toml_library(self):
        text = (self.home / "config.toml").read_text()
        fields = {"name": "Demo", "base_url": "https://api.demo.com/v1", "wire_api": "chat"}
        new = configfile_module.upsert_provider_block(text, "demo", fields, None)
        self.assertIn("[model_providers.demo]", new)
        self.assertIn('base_url = "https://api.demo.com/v1"', new)
        self.assertIn("[mcp_servers.example]", new)

    def test_guard_notices_any_other_change(self):
        text = (self.home / "config.toml").read_text()
        tampered = text.replace('appearanceTheme = "dark"', 'appearanceTheme = "light"')
        self.assertNotEqual(
            configfile_module._text_without_blocks(text, set()).strip(),
            configfile_module._text_without_blocks(tampered, set()).strip(),
        )

    def test_read_top_level_stops_at_first_table(self):
        text = (self.home / "config.toml").read_text()
        values = configfile_module.read_top_level(text, ("model_provider", "appearanceTheme"))
        self.assertEqual(values.get("model_provider"), "openai")
        self.assertNotIn("appearanceTheme", values)


class WebUITests(TempCodexHome):
    """图形界面：静态资源必须带令牌，否则页面会变成没样式、永远转圈的空白页。"""

    def setUp(self) -> None:
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from codex_switcher.webui import server

        self.token = "test-token-123"
        server.Handler.token = self.token
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _get(self, path: str):
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def test_page_requires_token(self):
        status, _ = self._get("/")
        self.assertEqual(status, 403)

    def test_page_references_assets_with_token(self):
        status, body = self._get("/?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn("style.css?t=" + self.token, body)
        self.assertIn("app.js?t=" + self.token, body)

    def test_assets_are_served_with_token(self):
        status, body = self._get("/static/style.css?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn("--accent", body)
        # 弹窗设了 display:flex，必须显式处理 hidden，否则一进页面就盖住整个界面
        self.assertIn(".modal[hidden]", body)
        status, body = self._get("/static/app.js?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn("loadState", body)

    def test_assets_reject_missing_token(self):
        self.assertEqual(self._get("/static/app.js")[0], 403)

    def test_api_state_shape(self):
        status, body = self._get("/api/state?t=" + self.token)
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertIn("providers", document)
        self.assertIn("presets", document)
        self.assertIn("current", document)
        self.assertGreater(len(document["presets"]), 10)


class UsageTests(unittest.TestCase):
    def test_human_tokens(self):
        from codex_switcher import usage
        self.assertEqual(usage.human_tokens(950), "950")
        self.assertEqual(usage.human_tokens(1500), "1.5K")
        self.assertEqual(usage.human_tokens(2300000), "2.3M")


if __name__ == "__main__":
    unittest.main(verbosity=2)
