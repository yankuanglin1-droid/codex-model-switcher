"""核心逻辑测试。不联网、不碰真实配置：全部跑在临时 CODEX_HOME 里。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_switcher import configfile as configfile_module  # noqa: E402

TOML_OK = configfile_module.toml_available()

_SESSION_HOME = None
_PREVIOUS_HOME = None


def setUpModule() -> None:
    """整个测试会话强制使用临时 CODEX_HOME。

    这条保险很关键：任何忘了继承 TempCodexHome 的用例都不会再碰用户真实的
    ~/.codex/config.toml —— 之前正是因为一个类的继承关系写错，把真实配置改掉了。
    """
    global _SESSION_HOME, _PREVIOUS_HOME
    _PREVIOUS_HOME = os.environ.get("CODEX_HOME")
    _SESSION_HOME = tempfile.mkdtemp(prefix="codex-switcher-tests-")
    os.environ["CODEX_HOME"] = _SESSION_HOME


def tearDownModule() -> None:
    if _PREVIOUS_HOME is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = _PREVIOUS_HOME


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
        # 后台全量迁移是守护线程，会和临时目录的清理打架；测试要求全同步
        from codex_switcher import engine as _engine
        self._old_allow_background = _engine.ALLOW_BACKGROUND_FOLLOW
        _engine.ALLOW_BACKGROUND_FOLLOW = False
        # 防呆：确认真的在临时目录里跑，而不是用户的家目录
        real_home = Path.home() / ".codex"
        self.assertNotEqual(Path(self._temp.name).resolve(), real_home.resolve())
        (self.home / "config.toml").write_text(SAMPLE_CONFIG)

    def tearDown(self) -> None:
        from codex_switcher import engine as _engine
        _engine.ALLOW_BACKGROUND_FOLLOW = self._old_allow_background

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

    def test_set_feature_creates_the_table_when_it_is_missing(self):
        """没有 [features] 段时新建，别把文件写坏。"""
        from codex_switcher import configfile
        text = 'model = "m"\n\n[desktop]\naccent = "#0169cc"\n'
        new = configfile.set_feature(text, "tool_search", False)
        parsed = load_document(new)
        self.assertEqual(parsed["features"]["tool_search"], False)
        # 原有内容一字不动
        self.assertEqual(parsed["model"], "m")
        self.assertEqual(parsed["desktop"]["accent"], "#0169cc")

    def test_set_feature_updates_in_place_and_keeps_the_rest(self):
        """已有 [features] 时只改那一行，同段其他键和其余段落都不能动。"""
        from codex_switcher import configfile
        text = ('model = "m"\n\n[features]\nmemories = true\napps = true\n'
                'tool_search = true\njs_repl = false\n\n[desktop]\naccent = "#0169cc"\n')
        new = configfile.set_feature(text, "tool_search", False)
        parsed = load_document(new)
        self.assertEqual(parsed["features"]["tool_search"], False)
        self.assertEqual(parsed["features"]["memories"], True)
        self.assertEqual(parsed["features"]["apps"], True)
        self.assertEqual(parsed["features"]["js_repl"], False)
        self.assertEqual(parsed["desktop"]["accent"], "#0169cc")
        # 只出现一次，不能重复插
        self.assertEqual(new.count("tool_search ="), 1)

    def test_set_feature_appends_when_the_key_is_absent(self):
        from codex_switcher import configfile
        text = '[features]\nmemories = true\n'
        new = configfile.set_feature(text, "tool_search", True)
        parsed = load_document(new)
        self.assertEqual(parsed["features"]["tool_search"], True)
        self.assertEqual(parsed["features"]["memories"], True)


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
        # deepseek-flash（V4.1-Flash）官方支持图片输入，本机实测数方块题 2/3 全对。
        # 这条曾经反着写（断言它不含 image），等于把阉割固化成了预期行为。
        self.assertIn("image", document["models"][1]["input_modalities"])

    def test_capability_switches_are_all_turned_on(self):
        """这几个字段在 Codex 里都带 #[serde(default)]，不写就是 false。

        少写一个就等于关掉一项能力：MCP / 插件 / 搜索 / 原图。
        注意：include_apps_usage_instructions 仅官方=True，第三方=False，
        否则 Codex 会把 tool_search 这类 OpenAI-only 工具塞给第三方，
        触发 `tools.N: tool type "tool_search" is not supported`（Kimi 实测）。
        """
        from codex_switcher import catalog
        # 官方模型：apps 开关应当开启
        entry_official = catalog.build_model_entry("MiniMax-M3", "Demo", is_official=True)
        for key in ("include_skills_usage_instructions",
                    "include_plugin_usage_instructions",
                    "include_apps_usage_instructions",
                    "supports_search_tool",
                    "supports_image_detail_original",
                    "supports_reasoning_summary_parameter"):
            self.assertTrue(entry_official[key],
                            "%s 官方必须为 true" % key)
        self.assertEqual(entry_official["web_search_tool_type"], "text_and_image")

        # 第三方模型：apps 开关必须关闭，其它照开
        entry_third = catalog.build_model_entry("MiniMax-M3", "Demo", is_official=False)
        self.assertFalse(entry_third["include_apps_usage_instructions"],
                         "第三方必须关闭 include_apps_usage_instructions，否则 tool_search 会触发 400")
        self.assertTrue(entry_third["include_skills_usage_instructions"])
        self.assertTrue(entry_third["include_plugin_usage_instructions"])
        # 第三方必须关 supports_search_tool，否则 Codex 会注册 tool_search
        self.assertFalse(entry_third["supports_search_tool"],
                         "第三方必须关闭 supports_search_tool，否则 spec_plan.rs:624 会注册 tool_search")
        # 用户 override 仍可强制打开
        entry_override = catalog.build_model_entry(
            "MiniMax-M3", "Demo", {"supports_search_tool": True}, is_official=False)
        self.assertTrue(entry_override["supports_search_tool"])
        # 官方必须开 supports_search_tool（否则 Codex 自家模型会缺搜索能力）
        entry_official_2 = catalog.build_model_entry("gpt-5-codex", "Demo", is_official=True)
        self.assertTrue(entry_official_2["supports_search_tool"])

    def test_text_only_model_keeps_image_switches_off(self):
        """纯文本模型才把图片相关的开关关掉，其余能力照给。"""
        from codex_switcher import catalog
        entry = catalog.build_model_entry("deepseek-v4-pro", "Demo")
        self.assertEqual(entry["input_modalities"], ["text"])
        self.assertFalse(entry["supports_image_detail_original"])
        self.assertEqual(entry["web_search_tool_type"], "text")
        # 但 MCP / 插件这些跟图片无关的能力不能跟着一起关掉
        self.assertTrue(entry["include_skills_usage_instructions"])

    def test_unknown_model_gets_full_capabilities(self):
        """没见过的模型默认给足能力，别默认成阉割版。

        注意：supports_search_tool 对第三方**默认 False**（spec_plan.rs:624
        的注册条件），「没见过的模型」也得遵守这个分平台规则。
        """
        from codex_switcher import catalog
        entry = catalog.build_model_entry("some-future-model-v9", "Demo")
        self.assertIn("image", entry["input_modalities"])
        self.assertTrue(entry["include_skills_usage_instructions"])
        # 默认走第三方路径：supports_search_tool 必须关，否则会注册 tool_search
        self.assertFalse(entry["supports_search_tool"])

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

    def test_preset_aliases(self):
        from codex_switcher import registry
        self.assertEqual(registry.preset("glm")["id"], "zhipu")
        self.assertEqual(registry.preset("GLM")["id"], "zhipu")
        self.assertEqual(registry.preset("kimi")["id"], "moonshot")
        self.assertEqual(registry.preset("ollama")["id"], "ollama-local")
        self.assertIsNone(registry.preset(""))
        self.assertIsNone(registry.preset("不存在的平台"))


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

    def test_record_without_adapter_falls_back_to_preset(self):
        """早期记录没挂余额适配器时，用内置预设兜底（DeepSeek 就是这样）。"""
        from codex_switcher import balance
        original = balance._http_json
        balance._http_json = lambda *a, **k: {
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "61.55",
                               "granted_balance": "0", "topped_up_balance": "61.55"}],
        }
        try:
            result = balance.query({"id": "deepseek", "balance": None}, "k")
        finally:
            balance._http_json = original
        self.assertEqual(result["status"], "ok")
        self.assertIn("61.55", result["display"])
        self.assertIn("platform.deepseek.com", result.get("console_url") or "")

    def test_unknown_record_stays_unsupported(self):
        from codex_switcher import balance
        result = balance.query({"id": "完全没有的平台", "balance": None}, "k")
        self.assertEqual(result["status"], "unsupported")


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
        # 全中文名字用短哈希，避免所有中文平台都叫同一个 id
        chinese = engine.slugify("中文平台")
        self.assertTrue(chinese.startswith("platform-"), chinese)
        self.assertNotEqual(engine.slugify("中文平台"), engine.slugify("另一个平台"))
        self.assertEqual(engine.slugify("中文平台"), engine.slugify("中文平台"))
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

    def test_switching_to_kimi_turns_tool_search_off_and_back_on(self):
        """Kimi（Moonshot）拒收 Codex 的 tool_search 内置工具，切到它必须关掉。

        真机两条报错都出自这里：
          · tools.13: tool type "tool_search" is not supported
          · cannot unmarshal object into ... arguments of type string
        """
        from codex_switcher import engine, paths, state as state_module

        def _record(provider_id, label):
            record = engine.build_provider_record(
                provider_id=provider_id, label=label,
                base_url="https://api.example.com/v1",
                models_url="https://api.example.com/v1/models",
                transport="native", requires_key=True)
            record["models"] = {"m1": {}}
            return record

        state_module.save({"schema_version": 3, "providers": {
            "moonshot": _record("moonshot", "Kimi"),
            "deepseek": _record("deepseek", "DeepSeek"),
        }})

        # 切到 Kimi：关掉
        engine.switch_to("moonshot", "m1")
        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["features"]["tool_search"], False)

        # 切到别的第三方：打开
        engine.switch_to("deepseek", "m1")
        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["features"]["tool_search"], True)

        # 切回官方：也要是打开的
        engine.switch_to("openai")
        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["features"]["tool_search"], True)

    def test_tool_search_switch_can_be_overridden_per_provider(self):
        """平台记录里写了 supports_tool_search 就以它为准。"""
        from codex_switcher import engine
        self.assertTrue(engine.tool_search_disabled({"id": "moonshot"}))
        self.assertFalse(engine.tool_search_disabled({"id": "deepseek"}))
        # 覆盖：Kimi 也能显式打开，别的平台也能显式关掉
        self.assertFalse(engine.tool_search_disabled({"id": "moonshot", "supports_tool_search": True}))
        self.assertTrue(engine.tool_search_disabled({"id": "deepseek", "supports_tool_search": False}))

    def test_switch_to_third_party_auto_cleans_old_sessions(self):
        """切换平台的副作用：把旧会话里别家的服务端工具条目自动剥掉。

        实测事故：MiniMax 跑 web_search 产出的 web_search_call 只有 id 没有
        call_id，切到 deepseek 后 resume 旧会话直接 400（missing field call_id）。
        用户不会记得手动跑 history --clean，所以这一步必须自动。
        """
        from codex_switcher import configfile, engine, history, paths
        from codex_switcher import state as state_module
        record = engine.build_provider_record(
            provider_id="deepseek", label="DeepSeek",
            base_url="https://api.deepseek.com",
            models_url="https://api.deepseek.com/models",
            transport="native", requires_key=False)
        record["models"] = {"deepseek-flash": {}}
        state_module.save({"schema_version": 3, "providers": {"deepseek": record}})

        # 造一份带 web_search_call 的旧会话（像 MiniMax 留下的那样）
        day = paths.sessions_dir() / "2026" / "09" / "17"
        day.mkdir(parents=True, exist_ok=True)
        rollout = day / "rollout-auto-switch.jsonl"
        payloads = [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "查一下 Pro x20"}]},
            {"type": "web_search_call", "id": "call_ccecac98e000400aaf9cfa",
             "status": "completed", "action": {"type": "search", "query": "Pro x20"}},
        ]
        rollout.write_text("\n".join(
            json.dumps({"type": "response_item", "payload": item}) for item in payloads)
            + "\n", encoding="utf-8")
        import os as _os
        import time as _time
        old = _time.time() - 3600
        _os.utime(rollout, (old, old))

        result = engine.switch_to("deepseek", "deepseek-flash")
        report = result.get("history_clean")
        self.assertIsNotNone(report)
        self.assertEqual(report["cleaned"], 1)
        self.assertNotIn("web_search_call", rollout.read_text(encoding="utf-8"))
        self.assertTrue(report["backup_dir"])

    def test_switching_to_openai_does_not_touch_history(self):
        """官方的解析器认自家的条目，切回官方时不该动历史。"""
        from codex_switcher import engine, paths
        from codex_switcher import state as state_module
        record = engine.build_provider_record(
            provider_id="deepseek", label="DeepSeek",
            base_url="https://api.deepseek.com",
            models_url="https://api.deepseek.com/models",
            transport="native", requires_key=False)
        record["models"] = {"deepseek-flash": {}}
        state_module.save({"schema_version": 3, "providers": {"deepseek": record}})
        engine.switch_to("deepseek", "deepseek-flash")   # 先切到第三方

        day = paths.sessions_dir() / "2026" / "09" / "17"
        day.mkdir(parents=True, exist_ok=True)
        rollout = day / "rollout-openai-back.jsonl"
        rollout.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "web_search_call", "id": "ws_9"}}) + "\n", encoding="utf-8")
        import os as _os
        import time as _time
        old = _time.time() - 3600
        _os.utime(rollout, (old, old))

        result = engine.switch_to(engine.OFFICIAL_PROVIDER)
        self.assertIsNone(result.get("history_clean"))
        self.assertIn("web_search_call", rollout.read_text(encoding="utf-8"))

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

    def test_legacy_record_without_transport_stays_direct(self):
        """旧状态文件里没有 transport 字段，必须按直连处理。

        这条是防回归：曾经因为默认成“走协议桥”，会把用户本来好用的配置
        改成指向 127.0.0.1。
        """
        from codex_switcher import engine, paths, state as state_module
        legacy = {
            "id": "glm",
            "label": "GLM",
            "base_url": "https://open.bigmodel.cn/api/v1",
            "requires_key": False,
            "models": {"glm-5.3": {}, "glm-5-turbo": {}},
        }
        state_module.save({"schema_version": 2, "providers": {"glm": legacy}})
        self.assertEqual(engine.resolve_transport(legacy), "native")
        self.assertEqual(engine.effective_base_url(legacy), "https://open.bigmodel.cn/api/v1")
        engine.switch_to("glm", "glm-5.3")
        block = load_document(paths.config_path().read_text())["model_providers"]["glm"]
        self.assertEqual(block["base_url"], "https://open.bigmodel.cn/api/v1")
        self.assertNotIn("127.0.0.1", block["base_url"])

    def test_legacy_record_without_id_or_base_url_uses_config(self):
        """最老的记录连 id、base_url 都没有，地址只能从 config.toml 里取回来。

        防回归：曾经因为取不到地址，直接把空字符串写进了 base_url。
        """
        from codex_switcher import configfile, engine, paths, state as state_module
        legacy = {
            "label": "GLM",
            "default_model": "glm-5.3",
            "models": {"glm-5.3": {}, "glm-turbo": {}},
            "requires_key": False,  # 本用例只验证地址解析，不碰钥匙串
        }
        state_module.save({"schema_version": 2, "providers": {"zhipu-demo": legacy}})
        config = paths.config_path()
        config.write_text(SAMPLE_CONFIG + (
            '\n[model_providers.zhipu-demo]\n'
            'name = "GLM"\n'
            'base_url = "https://open.bigmodel.cn/api/v1"\n'
            'wire_api = "responses"\n'))

        resolved = engine.resolve_base_url(legacy, config.read_text(), "zhipu-demo")
        self.assertEqual(resolved, "https://open.bigmodel.cn/api/v1")

        engine.switch_to("zhipu-demo", "glm-5.3")
        # 用 provider_block_value 断言，这样没有 tomllib 的 Python 3.9 也能跑
        written = configfile.provider_block_value(config.read_text(), "zhipu-demo", "base_url")
        self.assertEqual(written, "https://open.bigmodel.cn/api/v1")

    def test_switch_refuses_when_base_url_cannot_be_found(self):
        from codex_switcher import engine, state as state_module
        mystery = {"id": "mystery", "label": "Mystery", "requires_key": False,
                   "models": {"m": {}}}
        state_module.save({"schema_version": 3, "providers": {"mystery": mystery}})
        with self.assertRaises(engine.SwitchError) as caught:
            engine.switch_to("mystery", "m")
        self.assertIn("Base URL", str(caught.exception))

    def test_reserved_ids_are_renamed(self):
        from codex_switcher import engine, state as state_module
        state_module.save({"schema_version": 3, "providers": {}})
        state = state_module.load()
        self.assertEqual(engine.unique_id(state, "ollama"), "ollama-local")
        self.assertEqual(engine.unique_id(state, "openai"), "openai-local")
        self.assertEqual(engine.unique_id(state, "deepseek"), "deepseek")

    def test_quota_is_user_declared_and_optional(self):
        from codex_switcher import engine, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})

        # 没设额度时不能凭空给百分比
        detail = engine.usage_with_quota("demo", None, 250)
        self.assertNotIn("percent", detail)
        self.assertEqual(detail["used_tokens"], 250)

        engine.set_quota("demo", 1000)
        stored = state_module.get_provider(state_module.load(), "demo")
        self.assertEqual(stored["quota_tokens"], 1000)
        detail = engine.usage_with_quota("demo", 1000, 250)
        self.assertEqual(detail["percent"], 25.0)
        self.assertEqual(detail["remaining_tokens"], 750)

        # 用超了也只显示 100%，不出现负数
        detail = engine.usage_with_quota("demo", 100, 500)
        self.assertEqual(detail["percent"], 100.0)
        self.assertEqual(detail["remaining_tokens"], 0)

        engine.set_quota("demo", None)
        self.assertNotIn("quota_tokens", state_module.get_provider(state_module.load(), "demo"))

    def test_quota_rejects_bad_values(self):
        from codex_switcher import engine, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})
        with self.assertRaises(engine.SwitchError):
            engine.set_quota("demo", 0)
        with self.assertRaises(engine.SwitchError):
            engine.set_quota("nope", 100)


class UpdateTests(unittest.TestCase):
    def test_version_tuple(self):
        from codex_switcher import update
        self.assertEqual(update.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(update.version_tuple("1.10.0"), (1, 10, 0))
        self.assertEqual(update.version_tuple(""), (0,))
        self.assertGreater(update.version_tuple("1.10.0"), update.version_tuple("1.9.9"))

    def test_describe_outcomes(self):
        from codex_switcher import update
        self.assertIn("无法确认", update.describe({"status": "failed"}))
        self.assertIn("已是最新", update.describe(
            {"status": "ok", "up_to_date": True, "current": "1.0.0"}))
        self.assertIn("有新版本", update.describe(
            {"status": "ok", "up_to_date": False, "latest": "v2.0.0", "current": "1.0.0",
             "url": "https://example.com"}))

    def test_fresh_cache_is_rechecked_against_current_version(self):
        """刚升级完不能还显示旧版本号。"""
        import datetime
        from codex_switcher import __version__, paths, update
        paths.ensure_dir(paths.state_dir())
        paths.state_dir().joinpath("update.json").write_text(json.dumps({
            "status": "ok",
            "current": "0.0.1",                 # 缓存里是升级前的旧版本
            "latest": "v0.0.1",
            "up_to_date": True,
            "url": "https://example.com",
            "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }))
        result = update.check(force=False)      # 命中缓存，但应按当前版本重算
        self.assertEqual(result["current"], __version__)
        self.assertTrue(result["up_to_date"])   # 0.0.1 <= 当前版本
        self.assertIn(__version__, update.describe(result))

    def test_read_cache_also_reports_current_version(self):
        """界面是直接读缓存的，所以 read_cache 自己就得算对版本号。

        之前只在 check() 里重算，界面绕过了 check()，导致升级完界面还显示旧版本。
        """
        import datetime
        from codex_switcher import __version__, paths, update
        paths.ensure_dir(paths.state_dir())
        paths.state_dir().joinpath("update.json").write_text(json.dumps({
            "status": "ok",
            "current": "0.0.1",
            "latest": "v0.0.1",
            "up_to_date": True,
            "url": "https://example.com",
            "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }))
        cached = update.read_cache()
        self.assertIsNotNone(cached)
        self.assertEqual(cached["current"], __version__)
        self.assertTrue(cached["up_to_date"])

    def test_read_cache_survives_broken_file(self):
        """缓存文件写坏不能把界面搞崩。"""
        from codex_switcher import paths, update
        paths.ensure_dir(paths.state_dir())
        paths.state_dir().joinpath("update.json").write_text("{ 这不是 json")
        self.assertIsNone(update.read_cache())
        paths.state_dir().joinpath("update.json").write_text('["不是对象"]')
        self.assertIsNone(update.read_cache())


class EngineContinueTests(TempCodexHome):
    """切换相关的后续用例。

    必须继承 TempCodexHome：漏掉继承就会去改用户真实的 ~/.codex。
    setUpModule 里还有一道兜底。
    """

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

    def test_reasoning_effort_is_forwarded_to_upstream(self):
        """选了思考强度就得真的发出去，否则等于没选。"""
        from codex_switcher import bridge
        payload = bridge.responses_to_chat({
            "model": "m1",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "reasoning": {"effort": "high"},
        }, "moonshot")
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_reasoning_effort_uses_the_provider_dialect(self):
        """智谱要 thinking，MiniMax 要 reasoning_split，不能一律发 reasoning_effort。"""
        from codex_switcher import bridge
        glm = bridge.responses_to_chat(
            {"model": "m1", "input": [], "reasoning": {"effort": "high"}}, "zhipu")
        self.assertEqual(glm["thinking"], {"type": "enabled"})
        minimax = bridge.responses_to_chat(
            {"model": "m1", "input": [], "reasoning": {"effort": "high"}}, "minimax")
        self.assertTrue(minimax["reasoning_split"])

    def test_no_effort_means_no_thinking_field(self):
        from codex_switcher import bridge
        payload = bridge.responses_to_chat({"model": "m1", "input": []}, "moonshot")
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("thinking", payload)

    def test_images_survive_the_translation(self):
        """读图能力靠这一段：图片块必须原样转过去。"""
        from codex_switcher import bridge
        payload = bridge.responses_to_chat({
            "model": "m1",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "什么颜色"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ]}],
        }, "moonshot")
        self.assertEqual(payload["messages"][-1]["content"][1]["image_url"]["url"],
                         "data:image/png;base64,AAA")


class CapabilityTests(TempCodexHome):
    """模型能力：档位归一化、按平台翻译、实测结论写回目录。"""

    def _provider(self, provider_id="moonshot", models=("kimi-k2",), transport="native"):
        from codex_switcher import engine, state as state_module
        record = engine.build_provider_record(
            provider_id=provider_id, label=provider_id,
            base_url="https://example.invalid/v1",
            models_url="https://example.invalid/v1/models",
            transport=transport, requires_key=False)
        record["models"] = {name: {} for name in models}
        state = state_module.load()
        state.setdefault("providers", {})[provider_id] = record
        state_module.save(state)
        return record

    def test_effort_aliases_and_rejects_nonsense(self):
        from codex_switcher import capabilities
        self.assertEqual(capabilities.normalize_effort("HIGH"), "high")
        self.assertEqual(capabilities.normalize_effort("off"), "none")
        self.assertEqual(capabilities.normalize_effort("deep"), "high")
        self.assertIsNone(capabilities.normalize_effort("超强"))

    def test_thinking_payload_per_provider(self):
        from codex_switcher import capabilities
        self.assertEqual(capabilities.thinking_payload("moonshot", "high"),
                         {"reasoning_effort": "high"})
        self.assertEqual(capabilities.thinking_payload("zhipu", "high"),
                         {"thinking": {"type": "enabled"}})
        self.assertTrue(capabilities.thinking_payload("minimax", "high")["reasoning_split"])
        # 关掉思考就什么都不发
        self.assertEqual(capabilities.thinking_payload("moonshot", "none"), {})

    def test_matrix_marks_inferred_rows_as_inferred(self):
        from codex_switcher import capabilities
        rows = capabilities.matrix("moonshot", {"models": {"kimi-k2": {}}, "model_overrides": {}})
        self.assertEqual(rows[0]["model"], "kimi-k2")
        self.assertEqual(rows[0]["source"], "inferred")

    def test_verified_facts_outrank_the_guess(self):
        """实测过的模型必须显示实测结论，不能退回按名字猜。"""
        from codex_switcher import capabilities
        rows = capabilities.matrix("minimax", {
            "models": {"MiniMax-M3": {}}, "model_overrides": {}})
        self.assertEqual(rows[0]["source"], "verified")
        self.assertEqual(rows[0]["vision"], "yes")

    def test_apply_only_writes_what_was_measured(self):
        """没测出来的项（unknown）不能当成「不支持」写进去。"""
        from codex_switcher import capabilities
        record = {"models": {"m1": {}}, "model_overrides": {}}
        outcome = capabilities.apply_result("moonshot", record, "m1", {
            "vision": "no", "reasoning": "unknown", "tools": "yes"})
        entry = record["model_overrides"]["m1"]
        self.assertEqual(entry["input_modalities"], ["text"])
        self.assertEqual(entry["capabilities"]["vision"], "no")
        self.assertNotIn("reasoning", entry["capabilities"])
        self.assertEqual(entry["supports_parallel_tool_calls"], True)
        self.assertIn("读图", outcome["changed"])
        self.assertNotIn("思考", outcome["changed"])

    def test_apply_downgrades_a_blind_model(self):
        from codex_switcher import capabilities
        record = {"models": {"m1": {}}, "model_overrides": {}}
        capabilities.apply_result("moonshot", record, "m1", {"vision": "no"})
        entry = record["model_overrides"]["m1"]
        self.assertEqual(entry["input_modalities"], ["text"])

    def test_set_effort_updates_catalog_and_config(self):
        from codex_switcher import catalog, engine, paths, configfile
        self._provider("moonshot", ("kimi-k2",))
        paths.config_path().write_text(
            'model_provider = "moonshot"\nmodel = "kimi-k2"\n')
        result = engine.set_reasoning_effort("moonshot", "kimi-k2", "low")
        self.assertEqual(result["effort"], "low")
        entry = catalog.model_entry(catalog.catalog_path("moonshot"), "kimi-k2")
        self.assertEqual(entry["default_reasoning_level"], "low")
        # 正在用这个模型，配置必须跟着改，否则用户以为改了其实没生效
        parsed = load_document(paths.config_path().read_text())
        self.assertEqual(parsed["model_reasoning_effort"], "low")

    def test_set_effort_rejects_unknown_level(self):
        from codex_switcher import engine
        self._provider("moonshot", ("kimi-k2",))
        with self.assertRaises(engine.SwitchError):
            engine.set_reasoning_effort("moonshot", "kimi-k2", "变态强")

    def test_set_capability_rewrites_modalities(self):
        from codex_switcher import catalog, engine
        self._provider("minimax", ("MiniMax-M3",))
        engine.set_model_capability("minimax", "MiniMax-M3", vision=False)
        entry = catalog.model_entry(catalog.catalog_path("minimax"), "MiniMax-M3")
        self.assertEqual(entry["input_modalities"], ["text"])

    def test_manual_annotation_cycles_and_shows_its_source(self):
        """界面上点标签标注：三态可循环，来源标成「手动指定」。"""
        from codex_switcher import catalog, engine
        self._provider("minimax", ("MiniMax-M2",))

        engine.set_model_capability("minimax", "MiniMax-M2", key="vision", value="yes")
        rows = engine.capability_matrix("minimax")
        self.assertEqual(rows[0]["vision"], "yes")
        self.assertEqual(rows[0]["source"], "manual")
        entry = catalog.model_entry(catalog.catalog_path("minimax"), "MiniMax-M2")
        self.assertIn("image", entry["input_modalities"])

        engine.set_model_capability("minimax", "MiniMax-M2", key="vision", value="no")
        rows = engine.capability_matrix("minimax")
        self.assertEqual(rows[0]["vision"], "no")
        entry = catalog.model_entry(catalog.catalog_path("minimax"), "MiniMax-M2")
        self.assertEqual(entry["input_modalities"], ["text"])

        # 再点一下回到「未测出」：手写标注清掉，回落到按名字/官方推断的值
        engine.set_model_capability("minimax", "MiniMax-M2", key="vision", value="unknown")
        rows = engine.capability_matrix("minimax")
        self.assertNotEqual(rows[0].get("source"), "manual")

    def test_manual_annotation_rejects_nonsense(self):
        from codex_switcher import engine
        self._provider("minimax", ("MiniMax-M2",))
        with self.assertRaises(engine.SwitchError):
            engine.set_model_capability("minimax", "MiniMax-M2", key="telepathy", value="yes")
        with self.assertRaises(engine.SwitchError):
            engine.set_model_capability("minimax", "MiniMax-M2", key="vision", value="maybe")

    def test_manual_tools_annotation_flips_the_catalog_switch(self):
        from codex_switcher import catalog, engine
        self._provider("moonshot", ("kimi-k2",))
        engine.set_model_capability("moonshot", "kimi-k2", key="tools", value="no")
        entry = catalog.model_entry(catalog.catalog_path("moonshot"), "kimi-k2")
        self.assertFalse(entry["supports_parallel_tool_calls"])

    def test_measured_unknown_never_overrides_official_docs(self):
        """实测的「未测出」不能盖掉官方写明的结论。

        真实踩坑：deepseek-v4-pro 官方明确写「图像理解不支持」，我们发图片块
        被平台 400 拒掉，于是实测结论是 unknown —— 结果界面上显示成「未测出」，
        看着像谁都不知道，反而比官方文档还含糊。
        """
        from codex_switcher import capabilities
        row = capabilities.capability_row("deepseek", "deepseek-v4-pro")
        # 关键：读图这一项必须保留官方结论，不能被 unknown 盖成「未测出」
        self.assertEqual(row["vision"], "no")
        self.assertIn(row["source"], ("documented", "verified"))
        # 真正测出来的项照样采纳（tool/reasoning 实测为 yes）
        self.assertEqual(row["tools"], "yes")
        # 明确的冲突才记进 conflict：unknown 不算冲突
        self.assertNotIn("vision", row.get("conflict") or [])

    def test_documented_facts_carry_official_sources(self):
        """每条官方结论都必须带出处链接，否则以后没人能核对。"""
        from codex_switcher import capabilities
        self.assertTrue(capabilities.DOCUMENTED_FACTS)
        for key, fact in capabilities.DOCUMENTED_FACTS.items():
            self.assertIn("/", key)
            self.assertTrue(fact.get("url", "").startswith("http"), key)
            self.assertTrue(fact.get("source"), key)
        for pattern, fact in capabilities.DOCUMENTED_RULES:
            self.assertTrue(fact.get("url", "").startswith("http"), pattern)
            self.assertTrue(fact.get("source"), pattern)

    def test_platform_docs_explain_how_to_connect(self):
        """能力页要能回答「这个平台怎么配进 Codex」，所以接入说明不能空。"""
        from codex_switcher import capabilities
        for provider in ("minimax", "deepseek", "glm", "moonshot"):
            docs = capabilities.platform_docs(provider)
            self.assertTrue(docs, provider)
            self.assertTrue(docs["docs"].startswith("http"))
            self.assertTrue(docs["hint"])

    def test_probe_refuses_without_base_url(self):
        from codex_switcher import capabilities
        with self.assertRaises(capabilities.ProbeError):
            capabilities.probe("x", {"models": {"m": {}}, "transport": "native"},
                               "m", None)

    def test_capability_matrix_lists_every_provider(self):
        from codex_switcher import engine
        self._provider("moonshot", ("kimi-k2",))
        self._provider("minimax", ("MiniMax-M3",))
        rows = engine.capability_matrix()
        self.assertEqual({row["provider"] for row in rows}, {"moonshot", "minimax"})


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

    def test_state_payload_carries_effort_and_capability_data(self):
        """界面要让人选档位、看能力，数据得先送到前端。"""
        from codex_switcher import engine, state as state_module
        from codex_switcher.webui import server
        record = engine.build_provider_record(
            provider_id="minimax", label="MiniMax",
            base_url="https://example.invalid/v1",
            models_url="https://example.invalid/v1/models",
            transport="native", requires_key=False)
        record["models"] = {"MiniMax-M3": {}}
        state_module.save({"schema_version": 3, "providers": {"minimax": record}})
        engine.set_reasoning_effort("minimax", "MiniMax-M3", "high")

        payload = server._state_payload()
        item = next(row for row in payload["providers"] if row["id"] == "minimax")
        self.assertEqual(item["model_efforts"]["MiniMax-M3"]["current"], "high")
        self.assertEqual(item["model_capabilities"]["MiniMax-M3"]["vision"], "yes")
        self.assertEqual(item["model_capabilities"]["MiniMax-M3"]["source"], "verified")

    def test_page_references_assets_with_token(self):
        status, body = self._get("/?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn("style.css?t=" + self.token, body)
        self.assertIn("app.js?t=" + self.token, body)

    def test_every_static_reference_gets_a_token(self):
        """页面里引用的每个静态资源都必须带上令牌。

        这条是补出来的：以前服务端把 style.css / app.js / logo.svg 三个文件名
        写死在那里注入令牌，后来加了 i18n.js 忘了补，浏览器取不到令牌被 403
        挡掉，界面直接报 "toggleLang is not defined"。所以改成从 index.html
        自己扫一遍引用，新增资源忘了处理就会被这条测试拦住。
        """
        import re as _re
        from pathlib import Path as _Path
        static_index = _Path(__file__).resolve().parents[1] / "codex_switcher/webui/static/index.html"
        referenced = _re.findall(r'(?:src|href)="(/static/[^"?]+)"', static_index.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(referenced), 4, "页面里的静态引用比预期少，检查是否漏读")
        status, body = self._get("/?t=" + self.token)
        self.assertEqual(status, 200)
        for ref in referenced:
            self.assertIn("%s?t=%s" % (ref, self.token), body, "%s 没带上令牌" % ref)

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
        self.assertIn("project_url", document)
        self.assertIn("update", document)
        self.assertGreater(len(document["presets"]), 10)

    def test_quota_endpoint_round_trip(self):
        from codex_switcher import engine, state as state_module
        record = engine.build_provider_record(
            provider_id="demo", label="Demo", base_url="https://api.demo.com/v1",
            models_url="", requires_key=False)
        record["models"] = {"a": {}}
        state_module.save({"schema_version": 3, "providers": {"demo": record}})

        import urllib.request
        url = "http://127.0.0.1:%d/api/quota?t=%s" % (self.port, self.token)
        body = json.dumps({"provider": "demo", "tokens": 1000}).encode()
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            document = json.loads(response.read().decode())
        item = [p for p in document["state"]["providers"] if p["id"] == "demo"][0]
        self.assertEqual(item["usage"]["quota_tokens"], 1000)
        self.assertIn("used_tokens_human", item["usage"])


class UsageTests(unittest.TestCase):
    def test_human_tokens(self):
        from codex_switcher import usage
        self.assertEqual(usage.human_tokens(950), "950")
        self.assertEqual(usage.human_tokens(1500), "1.5K")
        self.assertEqual(usage.human_tokens(2300000), "2.3M")


class SafetyTests(unittest.TestCase):
    """防止测试误改用户真实配置的保险，必须有。"""

    def test_codex_home_is_temporary_during_tests(self):
        from codex_switcher import paths
        real = (Path.home() / ".codex").resolve()
        self.assertNotEqual(paths.codex_home().resolve(), real,
                            "测试期间 CODEX_HOME 必须指向临时目录，否则会改到用户的真实配置")

    def test_session_home_is_set_by_module(self):
        self.assertIsNotNone(_SESSION_HOME)
        self.assertTrue(str(os.environ.get("CODEX_HOME", "")).startswith(_SESSION_HOME))


class PlatformTests(unittest.TestCase):
    """跨平台回归：Windows / 低版本 macOS 上也要能跑。"""

    def test_modules_import_without_unix_only_modules(self):
        """模拟 Windows：把 fcntl / termios / pwd / grp / resource / msvcrt 全屏蔽，
        所有模块仍然必须能导入 —— 早期版本直接在 engine.py 里 import fcntl，
        Windows 上连启动都起不来。

        用子进程跑，避免污染已经导入的模块状态。
        """
        import subprocess
        script = (
            "import sys\n"
            "for name in ['fcntl', 'termios', 'pwd', 'grp', 'resource', 'msvcrt']:\n"
            "    sys.modules[name] = None\n"
            "import importlib\n"
            "for module in ['codex_switcher.engine', 'codex_switcher.cli', 'codex_switcher.bridge',\n"
            "               'codex_switcher.threads', 'codex_switcher.secrets', 'codex_switcher.install',\n"
            "               'codex_switcher.platform_compat', 'codex_switcher.webui.server']:\n"
            "    importlib.import_module(module)\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, (result.stderr or "")[-600:])
        self.assertIn("ok", result.stdout)

    def test_platform_helpers_exist(self):
        from codex_switcher import platform_compat as pc
        self.assertTrue(pc.python_ok((3, 9, 0)))
        self.assertTrue(pc.python_ok((3, 12, 0)))
        self.assertFalse(pc.python_ok((3, 8, 0)))
        self.assertTrue(str(pc.runtime_dir()))
        # 锁在本平台可用
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            with pc.file_lock(Path(temp) / "x.lock"):
                pass

    def test_windows_file_lock_retries_instead_of_giving_up(self):
        """Windows 分支在 macOS 上跑不到，所以拿假 msvcrt 把逻辑走一遍。

        msvcrt.LK_LOCK 只会重试 10 次就抛异常；界面 / 协议桥 / 命令行三方抢锁时
        可能刚好撞上。现在改成自己重试，这里验证“前几次抢不到、后来抢到”能成功。
        """
        import sys
        import tempfile
        import types
        from codex_switcher import platform_compat as pc

        calls = {"lock": 0, "unlock": 0}

        def fake_locking(fd, mode, nbytes):
            if mode == fake.LK_NBLCK:
                calls["lock"] += 1
                if calls["lock"] < 3:          # 前两次被别的进程占着
                    raise OSError("locked")
            else:
                calls["unlock"] += 1

        fake = types.ModuleType("msvcrt")
        fake.LK_NBLCK, fake.LK_UNLCK, fake.locking = 1, 2, fake_locking

        original_module = sys.modules.get("msvcrt")
        original_flag = pc.IS_WINDOWS
        sys.modules["msvcrt"] = fake
        pc.IS_WINDOWS = True
        try:
            with tempfile.TemporaryDirectory() as temp:
                with pc.file_lock(Path(temp) / "w.lock"):
                    pass
            self.assertEqual(calls["lock"], 3, "应该重试到第三次才成功")
            self.assertEqual(calls["unlock"], 1, "退出时必须解锁")
        finally:
            pc.IS_WINDOWS = original_flag
            if original_module is None:
                sys.modules.pop("msvcrt", None)
            else:
                sys.modules["msvcrt"] = original_module

    def test_credential_helper_is_portable(self):
        """凭据助手在 Windows 上要用解释器去跑 .py，不能直接当可执行文件。"""
        from codex_switcher import platform_compat as pc, secrets
        command, arguments = secrets.helper_command("demo")
        if pc.IS_WINDOWS:
            self.assertTrue(command.lower().endswith(("python.exe", "pythonw.exe", "python")))
        else:
            self.assertEqual(command, str(secrets.install_helper()))
        self.assertIn("demo", arguments)
        source = secrets.helper_source()
        self.assertIn("sys.path.insert", source)
        compile(source, "helper", "exec")  # 生成的脚本必须语法正确


class AppEnsureBridgeTests(unittest.TestCase):
    """打开界面时要顺手把协议桥拉起来。

    macOS 靠 launch.sh 拉桥，Windows 是桌面快捷方式直接跑 app，没人管桥，
    走桥的平台就会连不上。这几条钉住「该起的时候起、不该起的时候别白起」。
    """

    def _run_ensure(self, overview, running):
        from codex_switcher import cli, engine, platform_compat
        spawned = []
        original_overview = engine.provider_overview
        original_running = cli.bridge.is_running if hasattr(cli, "bridge") else None
        original_spawn = platform_compat.spawn_detached
        engine.provider_overview = lambda: overview
        platform_compat.spawn_detached = lambda command, log_path=None: spawned.append(command) or 0
        try:
            import codex_switcher.bridge as bridge_module
            bridge_module.is_running = lambda port=8787: running
            cli._ensure_bridge()
        finally:
            engine.provider_overview = original_overview
            platform_compat.spawn_detached = original_spawn
            if original_running is not None:
                bridge_module.is_running = original_running
        return spawned

    def test_no_bridge_provider_does_not_spawn(self):
        spawned = self._run_ensure([{"id": "a", "transport": "native"}], running=False)
        self.assertEqual(spawned, [], "没有平台需要桥，不该白起一个进程")

    def test_bridge_provider_spawns_when_not_running(self):
        spawned = self._run_ensure([{"id": "a", "transport": "bridge"}], running=False)
        self.assertEqual(len(spawned), 1, "有平台需要桥且桥没跑，必须拉起来")
        self.assertIn("codex_switcher.bridge", spawned[0])

    def test_bridge_provider_does_not_spawn_twice(self):
        spawned = self._run_ensure([{"id": "a", "transport": "bridge"}], running=True)
        self.assertEqual(spawned, [], "桥已经在跑就不要重复起")

    def test_overview_failure_does_not_break_opening_ui(self):
        from codex_switcher import cli, engine, platform_compat
        original = engine.provider_overview
        engine.provider_overview = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            cli._ensure_bridge()      # 不能抛出去，界面必须还能打开
        finally:
            engine.provider_overview = original


class HistoryTests(TempCodexHome):
    """会话历史清洗：只删确定坏的，改前备份，正在写的文件不碰。

    这个功能会改用户的会话文件，所以每条边界都要钉住。
    """

    def _write_rollout(self, lines, name="rollout-test.jsonl"):
        from codex_switcher import paths
        day = paths.sessions_dir() / "2026" / "09" / "16"
        day.mkdir(parents=True, exist_ok=True)
        path = day / name
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        return path

    def _sample(self):
        return [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "hi"}]}},
            # 坏：工具结果没有 call_id —— 服务端会直接 400
            {"type": "response_item", "payload": {"type": "function_call_output",
                                                  "id": "fco_1", "output": "done"}},
            # 好：有 call_id
            {"type": "response_item", "payload": {"type": "function_call_output",
                                                  "call_id": "call_1", "output": "ok"}},
            # OpenAI 专有推理
            {"type": "response_item", "payload": {"type": "reasoning",
                                                  "encrypted_content": "xxx", "summary": []}},
            {"type": "response_item", "payload": {"type": "function_call",
                                                  "call_id": "call_1", "name": "shell"}},
        ]

    @staticmethod
    def _backdate(path, seconds=3600):
        """把修改时间往前拨，模拟"这个对话早就静下来了"。

        不拨的话会命中"最近 120 秒还在写就别碰"的护栏，清理逻辑根本不会执行。
        """
        import os as _os
        import time as _time
        old = _time.time() - seconds
        _os.utime(path, (old, old))
        return path

    def _cross_provider_sample(self):
        """一段带着 OpenAI 服务端工具调用的会话。

        这类条目（网页搜索、用电脑、生图、代码解释器）由 OpenAI 那边执行，
        第三方平台既不认识也不支持，整个请求会被拒。
        """
        return [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call",
                                                  "call_id": "ctc_1", "name": "codex_app.foo"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                                                  "call_id": "ctc_1", "output": "done"}},
            {"type": "response_item", "payload": {"type": "web_search_call", "id": "ws_1"}},
            # 这些是普通工具，不该被动
            {"type": "response_item", "payload": {"type": "function_call",
                                                  "call_id": "call_1", "name": "shell"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                                                  "call_id": "call_1", "output": "ok"}},
        ]

    def test_cross_provider_items_are_reported_by_default(self):
        from codex_switcher import history
        info = history.inspect(self._write_rollout(self._cross_provider_sample(),
                                                   name="rollout-cross.jsonl"))
        self.assertEqual(info["cross_provider"], 3)
        # 默认不算「脏」——留在官方平台上是有效的，不该被误删
        self.assertFalse(history.is_dirty(info))
        self.assertTrue(history.is_dirty(info, cross_provider=True))

    def test_cross_provider_removes_calls_and_outputs_as_pairs(self):
        """只删一半会留下悬空引用，比不删更糟。必须成对消失。"""
        from codex_switcher import history
        path = self._write_rollout(self._cross_provider_sample(), name="rollout-cross.jsonl")
        self._backdate(path)
        backup = history._backup_root() / "test"
        changed, stats = history.sanitize(path, False, backup, cross_provider=True)
        self.assertTrue(changed)
        self.assertEqual(stats["removed_cross_provider"], 3)

        after = history.inspect(path)
        self.assertEqual(after["cross_provider"], 0)
        # 普通工具调用必须原样保留
        text = path.read_text(encoding="utf-8")
        self.assertIn('"call_1"', text)
        self.assertNotIn("codex_app.foo", text)

    def test_tool_search_items_are_stripped_for_strict_providers(self):
        """Codex 的 tool_search：平台不认工具类型，也解析不了它的对象型 arguments。

        真机报错对：
          · tools.13: tool type "tool_search" is not supported
          · cannot unmarshal object into Go struct field alias.arguments of type string
        根子是同一个：tool_search_call 的 arguments 是**对象**，
        而平台按 function_call 的约定要求它是字符串。
        """
        from codex_switcher import history
        sample = [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "hi"}]}},
            # 注意 arguments 是 dict，不是字符串 —— 和 function_call 不一样
            {"type": "response_item", "payload": {"type": "tool_search_call",
                                                  "id": "ts_1", "status": "completed",
                                                  "arguments": {"query": "jianying"}}},
            {"type": "response_item", "payload": {"type": "tool_search_output",
                                                  "call_id": "ts_1", "execution": "client"}},
            # 普通工具不受影响
            {"type": "response_item", "payload": {"type": "function_call",
                                                  "call_id": "call_keep", "name": "shell",
                                                  "arguments": '{"a": 1}'}},
        ]
        path = self._write_rollout(sample, name="rollout-tool-search.jsonl")
        self._backdate(path)

        # 先确认它会被报出来
        self.assertEqual(history.inspect(path)["cross_provider"], 2)
        self.assertIn("tool_search_call", history.inspect(path)["report_only"])

        changed, stats = history.sanitize(path, False, history._backup_root() / "test",
                                          cross_provider=True)
        self.assertTrue(changed)
        self.assertEqual(stats["removed_cross_provider"], 2)

        after = history.inspect(path)
        self.assertEqual(after["cross_provider"], 0)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("tool_search", text)
        # 成对删：call 和 output 一起没了，不会留悬空引用
        self.assertNotIn("ts_1", text.split("tool_search")[0])
        self.assertIn('"call_keep"', text)

    def test_tool_search_items_are_left_alone_without_cross_provider(self):
        """默认模式只清"任何平台都不认"的孤儿，tool_search 属于"搬到第三方才清"。"""
        from codex_switcher import history
        sample = [
            {"type": "response_item", "payload": {"type": "tool_search_call",
                                                  "id": "ts_1", "arguments": {"query": "x"}}},
            {"type": "response_item", "payload": {"type": "tool_search_output",
                                                  "call_id": "ts_1"}},
        ]
        path = self._write_rollout(sample, name="rollout-tool-search-keep.jsonl")
        self._backdate(path)
        changed, _ = history.sanitize(path, False, history._backup_root() / "test")
        self.assertFalse(changed)

    def test_without_the_flag_they_survive(self):
        """没说要搬平台，就一个都不许删。"""
        from codex_switcher import history
        path = self._write_rollout(self._cross_provider_sample(), name="rollout-keep.jsonl")
        self._backdate(path)
        changed, stats = history.sanitize(path, False, history._backup_root() / "test")
        self.assertFalse(changed)
        self.assertEqual(stats["removed_cross_provider"], 0)
        self.assertEqual(history.inspect(path)["cross_provider"], 3)

    def test_inspect_finds_only_real_problems(self):
        from codex_switcher import history
        info = history.inspect(self._write_rollout(self._sample()))
        self.assertEqual(info["orphan_outputs"], 1)
        self.assertEqual(info["openai_only"], 1)
        self.assertTrue(history.is_dirty(info))

    def test_clean_removes_problems_and_keeps_the_rest(self):
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._sample()))
        report = history.clean([path], moving_off_openai=True)
        self.assertEqual(report["removed"]["orphan_outputs"], 1)
        self.assertEqual(report["removed"]["openai_only"], 1)
        left = [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        kinds = [l["payload"]["type"] for l in left]
        self.assertIn("message", kinds)                  # 正常对话不能动
        self.assertIn("function_call", kinds)            # 配对的调用不能动
        self.assertEqual(kinds.count("function_call_output"), 1)   # 只留下带 call_id 的那条
        self.assertNotIn("reasoning", kinds)
        self.assertTrue(Path(report["backup_dir"]).exists(), "必须留备份")

    def test_moving_off_openai_false_keeps_reasoning(self):
        """留在官方 OpenAI 时不能删推理条目 —— 官方文档说这些是要保留的。"""
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._sample()))
        report = history.clean([path], moving_off_openai=False)
        self.assertEqual(report["removed"]["openai_only"], 0)
        self.assertEqual(report["removed"]["orphan_outputs"], 1)
        kinds = [json.loads(l)["payload"]["type"]
                 for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        self.assertIn("reasoning", kinds)

    def test_dry_run_changes_nothing(self):
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._sample()))
        before = path.read_text(encoding="utf-8")
        report = history.clean([path], moving_off_openai=True, dry_run=True)
        self.assertEqual(report["changed"], 1)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_recently_written_file_is_skipped(self):
        """正在被写入的会话不能改 —— 那多半是用户当前开着的对话。"""
        import os as _os
        import time as _time
        from codex_switcher import history
        path = self._write_rollout(self._sample())
        _os.utime(path, (_time.time(), _time.time()))     # 刚写过
        report = history.clean([path], moving_off_openai=True)
        self.assertEqual(report["changed"], 0)
        self.assertEqual(len(report["skipped_active"]), 1)
        self.assertEqual(history.inspect(path)["orphan_outputs"], 1)   # 原样没动

    def test_clean_result_is_still_valid_json_lines(self):
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._sample()))
        history.clean([path], moving_off_openai=True)
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                json.loads(line)      # 解不动就抛

    # ---------------------------------------------------------- 自动清洗

    def test_auto_clean_strips_cross_provider_items_and_backs_up(self):
        """切换平台后自动执行：别家的服务端工具条目必须消失，且留有备份。"""
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._cross_provider_sample(),
                                                  name="rollout-auto1.jsonl"))
        before = path.read_text(encoding="utf-8")
        report = history.auto_clean(moving_off_openai=False)

        self.assertEqual(report["cleaned"], 1)
        self.assertEqual(report["removed"]["cross_provider"], 3)
        self.assertTrue(report["backup_dir"])
        self.assertTrue(Path(report["backup_dir"]).exists())
        after = path.read_text(encoding="utf-8")
        self.assertLess(len(after), len(before))
        # web_search_call 与 custom_tool_call 成对消失，普通工具调用原样保留
        self.assertNotIn("web_search_call", after)
        self.assertNotIn("custom_tool_call", after)
        self.assertIn("call_1", after)
        # 备份里还留着原文，随时能回溯
        backups = list(Path(report["backup_dir"]).glob("*" + path.name))
        self.assertEqual(len(backups), 1)
        self.assertIn("web_search_call", backups[0].read_text(encoding="utf-8"))

    def test_auto_clean_remembers_what_it_already_cleaned(self):
        """账本缓存：清过的文件不再动，切换才能秒回。"""
        from codex_switcher import history
        path = self._backdate(self._write_rollout(self._cross_provider_sample(),
                                                  name="rollout-auto2.jsonl"))
        history.auto_clean(moving_off_openai=False)
        second = history.auto_clean(moving_off_openai=False)
        self.assertEqual(second["cleaned"], 0)      # 账本命中，直接跳过
        self.assertEqual(second["checked"], 0)
        # 文件再被写过（Codex 追加了新的对话）就要重新检查。
        # 追加后先"放凉"：刚写完的文件会命中"还在写就不碰"的护栏，那是另一条边界。
        path.write_text(path.read_text(encoding="utf-8")
                        + json.dumps({"type": "response_item",
                                      "payload": {"type": "web_search_call",
                                                  "id": "ws_2"}}) + "\n",
                        encoding="utf-8")
        self._backdate(path)
        third = history.auto_clean(moving_off_openai=False)
        self.assertEqual(third["cleaned"], 1)

    def test_auto_clean_leaves_the_file_it_is_still_writing(self):
        """正在写，就是活的会话：不碰，只在报告里说一声。"""
        from codex_switcher import history
        path = self._write_rollout(self._cross_provider_sample(),
                                   name="rollout-auto3.jsonl")
        original = path.read_text(encoding="utf-8")
        report = history.auto_clean(moving_off_openai=False)
        self.assertEqual(report["skipped_active"], 1)
        self.assertEqual(report["cleaned"], 0)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_auto_clean_respects_its_time_budget(self):
        """时间预算耗尽就收工，剩下的下次接着清 —— 不能让切换卡在扫盘上。"""
        from codex_switcher import history
        self._backdate(self._write_rollout(self._cross_provider_sample(),
                                           name="rollout-auto4.jsonl"))
        report = history.auto_clean(moving_off_openai=False, budget_seconds=0)
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(report["cleaned"], 0)


class SweepTests(TempCodexHome):
    """全量清扫（history.sweep_all）。

    这条路径存在的理由必须被钉住：缺 call_id 的孤儿工具结果散落在**任意**
    老会话文件里，只扫"最近 N 个"永远扫不到它们，而用户点开哪个就炸哪个。
    真实机器上的数字：1347 个会话文件、33.9GB，其中 45 个文件带 72 条孤儿，
    最老的一条躺在 7 月底的小文件里。
    """

    def _rollout(self, name, lines, seconds_ago=3600, day=("2025", "01", "02")):
        from codex_switcher import paths
        folder = paths.sessions_dir().joinpath(*day)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        old = time.time() - seconds_ago
        os.utime(path, (old, old))
        return path

    @staticmethod
    def _orphan():
        """真实形态：codex_app 命名空间的工具只写了输出，没有 call_id。

        病根是 Codex 两套类型定义不对称 —— 会话文件里（ResponseItem）
        ``call_id: Option<String>`` 且 skip_if_none，可以缺省；发给 API 时
        （ResponseInputItem）``call_id: String``，必填。于是这条记录能存进
        文件，一旦被回放（继续对话，甚至后台"生成线程描述"的结构化回合）
        服务端就 400。
        """
        return {"type": "response_item", "payload": {
            "type": "function_call_output", "id": "fco_01a0a804-12fd-7401-9145",
            "name": "automation_update", "namespace": "codex_app",
            "output": "Automation: Obsidian + LLM Wiki 每日分层更新"}}

    @staticmethod
    def _healthy():
        return [
            {"type": "response_item", "payload": {"type": "function_call",
                                                  "call_id": "call_1", "name": "shell"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                                                  "call_id": "call_1", "output": "ok"}},
        ]

    def test_sweep_reaches_files_the_recent_window_never_sees(self):
        """孤儿躺在最老的文件里也要清掉 —— 这正是以前反复复发的原因。"""
        from codex_switcher import history
        dirty = self._rollout("rollout-old-dirty.jsonl", self._healthy() + [self._orphan()],
                              seconds_ago=90 * 24 * 3600)
        # 再堆一批更新的干净文件，把脏文件挤出"最近 N 个"的窗口
        for index in range(40):
            self._rollout("rollout-fresh-%02d.jsonl" % index, self._healthy(),
                          seconds_ago=200 + index)
        self.assertNotIn(dirty, history.recent_rollouts(30))

        report = history.sweep_all()
        self.assertEqual(report["removed"]["orphan_outputs"], 1)
        self.assertEqual(history.inspect(dirty)["orphan_outputs"], 0)
        text = dirty.read_text(encoding="utf-8")
        self.assertNotIn("automation_update", text)
        self.assertIn('"call_1"', text)          # 正常工具调用不许误伤
        self.assertTrue(report["backup_dir"])

    def test_sweep_skips_a_file_codex_still_holds_open(self):
        """Codex 攥着句柄的文件不能动，哪怕它已经半天没动静。"""
        from codex_switcher import history
        path = self._rollout("rollout-open.jsonl", [self._orphan()], seconds_ago=7200)
        original = history._codex_open_rollouts
        history._codex_open_rollouts = lambda: {str(path)}
        try:
            report = history.sweep_all()
        finally:
            history._codex_open_rollouts = original
        self.assertEqual(report["cleaned"], 0)
        self.assertEqual(report["skipped_busy"], 1)
        self.assertEqual(history.inspect(path)["orphan_outputs"], 1)

    def test_busy_reason_tells_an_open_handle_from_a_stale_mtime(self):
        from codex_switcher import history
        path = self._rollout("rollout-busy.jsonl", [self._orphan()], seconds_ago=7200)
        original = history._codex_open_rollouts
        try:
            history._codex_open_rollouts = lambda: {str(path)}
            self.assertEqual(history.busy_reason(path), "codex-open")
            history._codex_open_rollouts = lambda: set()
            self.assertIsNone(history.busy_reason(path))   # 老 mtime + 没人开着 = 可以清
            history._codex_open_rollouts = lambda: None    # 问不出来 → 退回 mtime 兜底
            self.assertIsNone(history.busy_reason(path))
            os.utime(path, None)                          # 刚被写过 → 保守跳过
            self.assertEqual(history.busy_reason(path), "recent")
        finally:
            history._codex_open_rollouts = original

    def test_sweep_ledger_makes_the_next_pass_cheap(self):
        """账本认过的文件不再重读 —— 否则每轮都要把几十 GB 会话读一遍。"""
        from codex_switcher import history
        self._rollout("rollout-a.jsonl", [self._orphan()], seconds_ago=7200)
        self._rollout("rollout-b.jsonl", self._healthy(), seconds_ago=7200)
        first = history.sweep_all()
        self.assertEqual(first["cleaned"], 1)
        second = history.sweep_all()
        self.assertEqual(second["cleaned"], 0)
        self.assertEqual(second["checked"], 0)
        self.assertGreaterEqual(second["skipped_cached"], 2)

    def test_sweep_leaves_cross_provider_items_alone_by_default(self):
        """默认只清"任何平台都不认"的孤儿；别家服务端工具条目要显式才剥。"""
        from codex_switcher import history
        path = self._rollout("rollout-cross2.jsonl", [
            {"type": "response_item", "payload": {"type": "web_search_call", "id": "ws_1"}}],
            seconds_ago=7200)
        default = history.sweep_all()
        self.assertEqual(default["cleaned"], 0)
        self.assertEqual(history.inspect(path)["cross_provider"], 1)

        # 深度模式不能被账本挡住（账本按清洗力度分桶）
        deep = history.sweep_all(cross_provider=True)
        self.assertEqual(deep["removed"]["cross_provider"], 1)
        self.assertEqual(history.inspect(path)["cross_provider"], 0)

    def test_engine_dry_run_writes_nothing(self):
        from codex_switcher import engine, history
        path = self._rollout("rollout-dry.jsonl", [self._orphan()], seconds_ago=7200)
        report = engine.sweep_history(apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(history.inspect(path)["orphan_outputs"], 1)
        self.assertIsNone(report["backup_dir"])

    def test_describe_sweep_says_something_a_human_can_act_on(self):
        from codex_switcher import history
        self._rollout("rollout-d.jsonl", [self._orphan()], seconds_ago=7200)
        text = history.describe_sweep(history.sweep_all())
        self.assertIn("call_id", text)
        self.assertIn("1", text)

    def test_sweep_never_touches_encrypted_reasoning_by_default(self):
        """默认清扫不许碰 ``encrypted_content`` 推理。

        实测本机 1290 / 1347 个文件带这类条目（11.6 万条），而用户正在用的
        deepseek 会话里就有 633 条，跑得好好的 —— 它被平台容忍，不是坏数据。
        只有明确"这个会话要搬到第三方"时才剥。
        """
        from codex_switcher import history
        path = self._rollout("rollout-reasoning.jsonl", [
            {"type": "response_item", "payload": {
                "type": "reasoning", "encrypted_content": "xxx", "summary": []}}],
            seconds_ago=7200)
        report = history.sweep_all()
        self.assertEqual(report["checked"], 1)     # 看过了
        self.assertEqual(report["cleaned"], 0)     # 但一条没动
        self.assertEqual(history.inspect(path)["openai_only"], 1)

        deep = history.sweep_all(moving_off_openai=True)
        self.assertEqual(deep["removed"]["openai_only"], 1)
        self.assertEqual(history.inspect(path)["openai_only"], 0)

    def test_dry_run_report_never_overstates(self):
        """干跑报告必须和"真的会删什么"一致，不能虚高。"""
        from codex_switcher import history
        self._rollout("rollout-dry-reasoning.jsonl", [
            {"type": "response_item", "payload": {
                "type": "reasoning", "encrypted_content": "xxx", "summary": []}}],
            seconds_ago=7200)
        report = history.sweep_all(dry_run=True)
        self.assertEqual(report["cleaned"], 0)
        self.assertEqual(report["items"], [])

    def test_switch_reports_the_full_sweep(self):
        """切换要顺手挂上全量清扫，否则存量孤儿只能等用户自己发现。"""
        from codex_switcher import engine
        from codex_switcher import state as state_module
        record = engine.build_provider_record(
            provider_id="local-ollama", label="本地模型",
            base_url="http://127.0.0.1:11434/v1",
            models_url="http://127.0.0.1:11434/v1/models",
            transport="native", requires_key=False)
        record["models"] = {"qwen3:32b": {}}
        state_module.save({"schema_version": 3, "providers": {"local-ollama": record}})
        result = engine.switch_to("local-ollama", "qwen3:32b")
        self.assertIn("history_sweep", result)


class ContextGuardTests(TempCodexHome):
    """上下文窗口守卫。

    这是「切到第三方模型后反复压缩」的根治措施，所以每条阈值都拿
    真实事故里的数字钉住：会话 01a0a8cd 在 17 分钟内被压了 199 次，
    当时是 364,713 tokens 硬塞进 124,518 的可用窗口。
    """

    # 真实事故数字
    REAL_TOKENS = 364713
    REAL_WINDOW = 124518          # 131072 × 0.95，与 Codex 上报的一致
    REAL_LIMIT = 74710            # 124518 × 0.6

    def _write_catalog(self, slug="deepseek-flash", context=131072, percent=95,
                       name="deepseek.json"):
        from codex_switcher import paths
        target = paths.catalog_dir() / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"models": [
            {"slug": slug, "context_window": context,
             "effective_context_window_percent": percent}]}), encoding="utf-8")
        return target

    def _write_rollout(self, total_tokens, name="rollout-guard.jsonl"):
        from codex_switcher import paths
        day = paths.sessions_dir() / "2026" / "09" / "17"
        day.mkdir(parents=True, exist_ok=True)
        path = day / name
        line = {"timestamp": "2026-09-17T02:55:18.161Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "model_context_window": self.REAL_WINDOW,
                    "last_token_usage": {"input_tokens": total_tokens - 200,
                                         "output_tokens": 200,
                                         "total_tokens": total_tokens}}}}
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        return path

    def test_effective_window_matches_what_codex_reports(self):
        """131072 × 0.95 必须等于 Codex 上报的 124518，差一个数就说明算法错了。"""
        from codex_switcher import contextguard
        self.assertEqual(
            contextguard.effective_window(
                {"context_window": 131072, "effective_context_window_percent": 95}),
            self.REAL_WINDOW)

    def test_effective_window_falls_back_to_95_percent(self):
        from codex_switcher import contextguard
        self.assertEqual(
            contextguard.effective_window({"context_window": 100000}),
            95000)

    def test_zero_or_garbage_window_is_not_a_window(self):
        from codex_switcher import contextguard
        for bad in ({"context_window": 0}, {"context_window": "abc"}, {}):
            self.assertEqual(contextguard.effective_window(bad), 0)

    def test_real_incident_is_judged_fork(self):
        """出事的那个会话必须被判成「装不下」，这是整个模块存在的理由。"""
        from codex_switcher import contextguard
        catalog = self._write_catalog()
        path = self._write_rollout(self.REAL_TOKENS)
        report = contextguard.assess(path, "deepseek-flash", catalog_path=catalog)
        self.assertEqual(report["tokens"], self.REAL_TOKENS)
        self.assertEqual(report["window"], self.REAL_WINDOW)
        self.assertEqual(report["action"], "fork")
        self.assertFalse(report["fits"])
        self.assertAlmostEqual(report["ratio"], 2.93, places=2)

    def test_small_thread_is_left_alone(self):
        from codex_switcher import contextguard
        catalog = self._write_catalog()
        path = self._write_rollout(30000)
        report = contextguard.assess(path, "deepseek-flash", catalog_path=catalog)
        self.assertEqual(report["action"], "continue")
        self.assertTrue(report["fits"])

    def test_tight_thread_asks_for_a_compact_first(self):
        from codex_switcher import contextguard
        catalog = self._write_catalog()
        path = self._write_rollout(100000)          # 124518 的 80%
        report = contextguard.assess(path, "deepseek-flash", catalog_path=catalog)
        self.assertEqual(report["action"], "compact-first")

    def test_auto_compact_limit_leaves_room_for_the_result(self):
        """压缩触发点必须明显小于窗口，否则压完还是超，又得再压一次。"""
        from codex_switcher import contextguard
        self.assertEqual(contextguard.auto_compact_limit(self.REAL_WINDOW), self.REAL_LIMIT)
        self.assertLess(self.REAL_LIMIT, self.REAL_WINDOW)

    def test_auto_compact_limit_never_goes_below_the_floor(self):
        from codex_switcher import contextguard
        self.assertGreaterEqual(contextguard.auto_compact_limit(100),
                                contextguard.MIN_AUTO_COMPACT_LIMIT)

    def test_unknown_window_never_claims_everything_is_fine(self):
        """拿不到窗口就老实说不知道，不许猜「没问题」。"""
        from codex_switcher import contextguard
        path = self._write_rollout(self.REAL_TOKENS)
        report = contextguard.assess(path, "some-unknown-model", catalog_path=None)
        self.assertEqual(report["action"], "unknown")
        self.assertIsNone(report["fits"])
        self.assertIsNone(report["auto_compact_limit"])

    def test_estimate_prefers_codex_own_token_count(self):
        from codex_switcher import contextguard
        path = self._write_rollout(123456)
        tokens, source = contextguard.estimate_thread_tokens(path)
        self.assertEqual(tokens, 123456)
        self.assertEqual(source, "token_count")

    def test_missing_file_reports_zero_not_a_crash(self):
        from codex_switcher import contextguard
        tokens, source = contextguard.estimate_thread_tokens(Path("/nope/nope.jsonl"))
        self.assertEqual(tokens, 0)
        self.assertEqual(source, "missing")

    def test_describe_says_something_a_human_can_act_on(self):
        from codex_switcher import contextguard
        catalog = self._write_catalog()
        path = self._write_rollout(self.REAL_TOKENS)
        text = contextguard.describe(
            contextguard.assess(path, "deepseek-flash", catalog_path=catalog))
        self.assertIn("124,518", text)
        self.assertIn("分叉", text)

    def test_provider_settings_writes_the_compact_limit(self):
        """切到第三方模型时，压缩触发点必须一起写进 config.toml。"""
        from codex_switcher import engine, paths
        self._write_catalog(name="deepseek.json")
        settings = engine.provider_settings(
            {"id": "deepseek", "label": "DeepSeek"}, "deepseek-flash", "deepseek")
        self.assertEqual(settings["model_auto_compact_token_limit"], self.REAL_LIMIT)
        # 官方模型不该被塞第三方那套东西
        official = engine.official_settings("gpt-5-codex")
        self.assertNotIn("model_auto_compact_token_limit", official)

    def test_compact_limit_is_a_key_codex_actually_accepts(self):
        """写进 config.toml 的键必须被认，否则 rewrite 会直接拒绝。"""
        from codex_switcher import configfile
        self.assertIn("model_auto_compact_token_limit", configfile.MANAGED_SET)

    def test_ui_payload_never_leaks_the_full_session_path(self):
        """界面要显示体检结果，但不能把完整会话路径送进浏览器。"""
        from codex_switcher.webui import server
        self._write_catalog()
        self._write_rollout(self.REAL_TOKENS)
        payload = server._guard_payload({"model": "deepseek-flash",
                                         "model_provider": "deepseek"})
        self.assertIsNotNone(payload)
        self.assertNotIn("path", payload)
        self.assertTrue(payload["thread"].startswith("rollout-"))
        self.assertEqual(payload["action"], "fork")

    def test_ui_payload_stays_quiet_when_it_knows_nothing(self):
        from codex_switcher.webui import server
        self.assertIsNone(server._guard_payload({}))

    def test_banner_strings_exist_in_both_languages(self):
        """中英文各一份，少一份英文界面就会显示原始 key。

        这类漂移已经出过一次（i18n.js 漏加 key 导致整页 JS 报错），钉住它。
        """
        source = Path(__file__).resolve().parents[1] / "codex_switcher" / "webui" / "static" / "i18n.js"
        text = source.read_text(encoding="utf-8")
        for key in ("guard.title", "guard.detail", "guard.note_fork",
                    "guard.note_compact", "guard.action_fork", "guard.action_compact"):
            self.assertEqual(text.count("'%s'" % key), 2, "key 数量不对：%s" % key)


class I18nTests(unittest.TestCase):
    """界面双语文案必须完整。

    i18n.js 里 zh / en 两份词典的键必须一一对应：少一个键，界面上就会出现
    key 本身或者退回中文，而且只在切到英文时才暴露。所以在这里静态比对。
    """

    def _dictionary_keys(self):
        import re as _re
        from pathlib import Path as _Path
        source = (_Path(__file__).resolve().parents[1]
                  / "codex_switcher/webui/static/i18n.js").read_text(encoding="utf-8")
        blocks = {}
        for name in ("zh", "en"):
            match = _re.search(r"\n  %s: \{(.*?)\n  \}," % name, source, _re.S)
            self.assertIsNotNone(match, "词典里找不到 %s 段" % name)
            blocks[name] = set(_re.findall(r"'([A-Za-z0-9_.]+)':", match.group(1)))
        return blocks

    def test_both_dictionaries_have_the_same_keys(self):
        blocks = self._dictionary_keys()
        self.assertGreater(len(blocks["zh"]), 80, "中文词典条目太少，可能没解析成功")
        missing_in_en = sorted(blocks["zh"] - blocks["en"])
        missing_in_zh = sorted(blocks["en"] - blocks["zh"])
        self.assertEqual(missing_in_en, [], "英文词典缺这些键：%s" % missing_in_en[:8])
        self.assertEqual(missing_in_zh, [], "中文词典缺这些键：%s" % missing_in_zh[:8])

    def test_html_only_uses_keys_that_exist(self):
        """页面里 data-i18n 引用的键必须在词典里，否则会原样显示 key。"""
        import re as _re
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parents[1]
        keys = self._dictionary_keys()["zh"]
        html = (root / "codex_switcher/webui/static/index.html").read_text(encoding="utf-8")
        used = set()
        for attr in ("data-i18n", "data-i18n-placeholder", "data-i18n-title"):
            used |= set(_re.findall(r'%s="([^"]+)"' % attr, html))
        self.assertTrue(used, "页面上没有任何 data-i18n 标记？")
        unknown = sorted(used - keys)
        self.assertEqual(unknown, [], "页面引用了词典里没有的键：%s" % unknown)

    def test_language_switch_is_wired_up(self):
        """语言按钮必须存在、必须连上切换函数，脚本也必须真被页面加载。"""
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parents[1]
        html = (root / "codex_switcher/webui/static/index.html").read_text(encoding="utf-8")
        app = (root / "codex_switcher/webui/static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="btn-lang"', html)
        self.assertIn('src="/static/i18n.js"', html)
        # i18n.js 必须在 app.js 之前加载，否则 app.js 执行时还没有 t()
        self.assertLess(html.index('src="/static/i18n.js"'), html.index('src="/static/app.js"'))
        self.assertIn("btn-lang", app)
        self.assertIn("langchange", app)


class OutputEncodingTests(unittest.TestCase):
    """中文 Windows 的输出不能把命令搞崩。

    控制台代码页 936 里没有 ✅ ❌ ✗ ⚠；一旦输出被重定向，Python 用 cp936
    编码，打印这些符号会抛 UnicodeEncodeError，把正常命令打断。
    """

    def test_symbols_do_not_crash_under_gbk(self):
        import subprocess
        script = (
            "from codex_switcher.__main__ import _make_output_never_crash\n"
            "_make_output_never_crash()\n"
            "print('✅ 已切换 ✗ 失败 ⚠ 注意')\n"
            "print('中文照常')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=dict(os.environ, PYTHONIOENCODING="cp936"),
            capture_output=True, timeout=60)
        # 子进程按 cp936 输出，这里不能按 UTF-8 解，否则是测试自己炸
        stderr = (result.stderr or b"").decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, stderr[-500:])
        self.assertNotIn("UnicodeEncodeError", stderr)

    def test_same_script_would_crash_without_the_guard(self):
        """证明前面那条测试不是白测的：不调兜底就会崩。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c", "print('✅')"],
            env=dict(os.environ, PYTHONIOENCODING="cp936"),
            capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UnicodeEncodeError", (result.stderr or b"").decode("utf-8", "replace"))


class ThreadBindingTests(TempCodexHome):
    """任务绑定：切完平台继续任务报 unknown model 这一类事故。

    实测：Codex 恢复旧任务时取的是任务自己记的服务商（会话文件里的
    session_meta / thread_settings），模型名却取当前配置里的那个。只改
    config.toml 不改任务绑定，请求就会带着新平台的模型名敲进旧平台的接口，
    服务端回 unknown model。所以旧任务必须跟着一起搬。
    """

    def _make_db(self, rows):
        import sqlite3
        db = self.home / "state_5.sqlite"
        connection = sqlite3.connect(str(db))
        connection.execute(
            "CREATE TABLE threads (id TEXT, title TEXT, model TEXT, model_provider TEXT,"
            " rollout_path TEXT, updated_at REAL, source TEXT)")
        for row in rows:
            connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", row)
        connection.commit()
        connection.close()
        catalog = self.home / "sqlite"
        catalog.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(catalog / "codex-dev.db"))
        connection.execute(
            "CREATE TABLE local_thread_catalog (thread_id TEXT, model_provider TEXT)")
        for row in rows:
            connection.execute("INSERT INTO local_thread_catalog VALUES (?,?)",
                               (row[0], row[3]))
        connection.commit()
        connection.close()
        return db

    def _make_rollout(self, name, provider):
        import time
        path = self.home / (name + ".jsonl")
        lines = [
            json.dumps({"type": "session_meta",
                        "payload": {"model_provider": provider, "id": name}}),
            json.dumps({"type": "turn_context", "payload": {"model": "whatever"}}),
            json.dumps({"type": "event_msg",
                        "payload": {"thread_settings": {"model_provider_id": provider}}}),
        ]
        path.write_text("\n".join(lines) + "\n")
        # 装成「早就不在写入」的样子，绕开活动文件保护
        old = time.time() - 3600
        os.utime(path, (old, old))
        return path

    def _thread_row(self, thread_id, model, provider, path, age=60.0, source=""):
        import time
        return (thread_id, "标题", model, provider, str(path), time.time() - age, source)

    def _read_thread(self, thread_id):
        import sqlite3
        connection = sqlite3.connect(str(self.home / "state_5.sqlite"))
        row = connection.execute(
            "SELECT model, model_provider FROM threads WHERE id = ?", (thread_id,)).fetchone()
        connection.close()
        return row

    def test_follow_switch_moves_the_whole_task_to_the_new_provider(self):
        from codex_switcher import threads
        path = self._make_rollout("task-a", "minimax")
        self._make_db([self._thread_row("aaa111", "MiniMax-M3", "minimax", path)])
        report = threads.follow_switch("minimax", "deepseek", "deepseek-flash")
        self.assertEqual(report["moved"], 1)
        model, provider = self._read_thread("aaa111")
        self.assertEqual(provider, "deepseek")
        # 模型名也得换：只换服务商不换模型，下次还是拿旧模型名敲新平台的门
        self.assertEqual(model, "deepseek-flash")
        text = path.read_text()
        self.assertIn('"model_provider":"deepseek"', text)
        self.assertIn('"model_provider_id":"deepseek"', text)
        self.assertNotIn("minimax", text)
        self.assertTrue(report["backup_dir"])

    def test_follow_switch_leaves_openai_tasks_alone(self):
        """ChatGPT 账号的任务不跟着搬：那是老家，用户多半还要切回来。"""
        from codex_switcher import threads
        path = self._make_rollout("task-b", "openai")
        self._make_db([self._thread_row("bbb222", "gpt-5.6-sol", "openai", path)])
        report = threads.follow_switch("openai", "deepseek", "deepseek-flash")
        self.assertEqual(report["moved"], 0)
        self.assertEqual(self._read_thread("bbb222")[1], "openai")

    def test_follow_switch_skips_a_file_codex_is_still_writing(self):
        """Codex 正在写的文件不能替换 inode，数据库照改，文件留到巡检补。"""
        from codex_switcher import threads
        path = self._make_rollout("task-c", "minimax")
        self._make_db([self._thread_row("ccc333", "MiniMax-M3", "minimax", path)])
        os.utime(path, None)  # 刚刚写过 → 活动文件
        report = threads.follow_switch("minimax", "deepseek", "deepseek-flash")
        self.assertEqual(report["moved"], 1)
        self.assertEqual(report["items"][0]["active"], True)
        self.assertEqual(self._read_thread("ccc333")[1], "deepseek")
        self.assertIn("minimax", path.read_text())

    def test_repair_fixes_a_stale_session_file_without_the_deep_flag(self):
        """数据库已经对了、文件里还留着旧服务商 —— 这是切换后继续任务报错的
        真实现场，以前只有 --deep 才管，默认得自动修掉。"""
        from codex_switcher import threads
        path = self._make_rollout("task-d", "minimax")
        self._make_db([self._thread_row("ddd444", "deepseek-flash", "deepseek", path)])
        report = threads.repair()
        self.assertEqual(report["fixed"], 1, report)
        self.assertNotIn("minimax", path.read_text())
        self.assertIn('"model_provider":"deepseek"', path.read_text())

    def test_repair_ignores_threads_without_a_provider(self):
        """服务商是空值时不能拿空串去对齐，否则会把会话文件里的值抹掉。"""
        from codex_switcher import threads
        path = self._make_rollout("task-e", "minimax")
        self._make_db([self._thread_row("eee555", "MiniMax-M3", "", path)])
        threads.repair()
        # 文件保持原样（json.dumps 默认带空格），没有被空串抹掉
        self.assertIn('"model_provider": "minimax"', path.read_text())

    def test_switch_to_moves_recent_tasks_onto_the_new_provider(self):
        """端到端：切换平台时，最近在用的任务跟着搬过去。"""
        from codex_switcher import engine, threads
        from codex_switcher import state as state_module
        path = self._make_rollout("task-f", "minimax")
        self._make_db([self._thread_row("fff666", "MiniMax-M3", "minimax", path)])
        state = state_module.load()
        for provider_id, label, url in (
                ("minimax", "MiniMax", "https://api.minimaxi.com/v1"),
                ("deepseek", "DeepSeek", "https://api.deepseek.com")):
            record = engine.build_provider_record(
                provider_id=provider_id, label=label, base_url=url,
                models_url=url + "/models", transport="native", requires_key=False)
            record["models"] = {"MiniMax-M3": {}} if provider_id == "minimax" \
                else {"deepseek-flash": {}}
            state_module.upsert_provider(state, record)
        state_module.save(state)
        # 先切到 minimax，再从 minimax 切到 deepseek
        engine.switch_to("minimax", "MiniMax-M3")
        result = engine.switch_to("deepseek", "deepseek-flash")
        followed = result.get("threads_followed") or {}
        self.assertEqual(followed.get("moved"), 1, followed)
        self.assertEqual(self._read_thread("fff666"), ("deepseek-flash", "deepseek"))

    # ---- 绑定改写必须连带清洗跨平台历史（missing field call_id 的根治） ----

    def _make_dirty_rollout(self, name, provider):
        """带 OpenAI 服务端工具条目的会话文件：搬到第三方平台必然 400。"""
        import time
        path = self._make_rollout(name, provider)
        lines = [
            json.dumps({"type": "response_item",
                        "payload": {"type": "web_search_call", "id": "ws_1"}}),
            json.dumps({"type": "response_item",
                        "payload": {"type": "reasoning", "encrypted_content": "zzz"}}),
            json.dumps({"type": "response_item",
                        "payload": {"type": "function_call_output", "output": "ok"}}),
            json.dumps({"type": "response_item",
                        "payload": {"type": "message",
                                    "content": [{"type": "output_text", "text": "hi"}]}}),
        ]
        with path.open("a") as stream:
            stream.write("\n".join(lines) + "\n")
        # 追加行会把 mtime 变新，重新装成「早已不在写入」的样子，绕开活动保护
        old = time.time() - 3600
        os.utime(path, (old, old))
        return path

    def test_repair_of_chatgpt_task_strips_openai_only_history(self):
        """ChatGPT 任务被修绑到第三方时，历史里的 OpenAI 专有条目必须同一次清掉。

        用户实测：ChatGPT 执行过的任务切到第三方继续，报
        missing field `call_id` —— 根因是 repair 只改了绑定、没洗历史。
        """
        from codex_switcher import threads
        path = self._make_dirty_rollout("task-gpt-dirty", "openai")
        self._make_db([self._thread_row("ggg777", "MiniMax-M3", "openai", path)])
        report = threads.repair()
        self.assertEqual(report["fixed"], 1, report)
        text = path.read_text()
        self.assertIn('"model_provider":"minimax"', text)
        # 这些条目在第三方平台上必然 400，改绑的同一事务里必须剥掉
        self.assertNotIn("web_search_call", text)
        self.assertNotIn("encrypted_content", text)
        self.assertNotIn("function_call_output", text)
        # 正常对话条目不能误伤
        self.assertIn('"output_text"', text)
        item = report["items"][0]
        self.assertEqual(item.get("history"), 3, item)

    def test_follow_switch_between_third_parties_strips_history_too(self):
        """第三方之间互搬：上一层平台的服务端工具条目同样必须剥掉。"""
        from codex_switcher import threads
        path = self._make_dirty_rollout("task-mini-dirty", "minimax")
        self._make_db([self._thread_row("hhh888", "MiniMax-M3", "minimax", path)])
        report = threads.follow_switch("minimax", "deepseek", "deepseek-flash")
        self.assertEqual(report["moved"], 1, report)
        text = path.read_text()
        self.assertNotIn("web_search_call", text)
        self.assertNotIn("function_call_output", text)
        self.assertIn('"model_provider":"deepseek"', text)
        # web_search_call + 孤儿 function_call_output，共 2 条
        self.assertEqual(report["items"][0].get("history"), 2, report["items"][0])

    def test_exec_scheduled_task_follows_switch_even_from_openai(self):
        """每日定时任务（source=exec）必须无条件跟随切换，哪怕来自 ChatGPT。

        它们不靠人点开、到点自动跑，绑定不对就是静默炸掉 —— 而且可能
        已经很久没更新过（36h 窗口罩不住），所以要单独豁免。
        """
        from codex_switcher import threads
        path = self._make_rollout("task-cron", "openai")
        # age = 7 天：远超 36h 窗口，普通任务不会被搬
        self._make_db([
            self._thread_row("cron01", "gpt-5-codex", "openai", path, age=7 * 86400,
                             source="exec"),
            self._thread_row("old02", "gpt-5-codex", "openai",
                             self.home / "old02.jsonl", age=7 * 86400),
        ])
        report = threads.follow_switch("openai", "minimax", "MiniMax-M3")
        self.assertEqual(report["moved"], 1, report)
        self.assertEqual(report["exec_followed"], 1)
        self.assertEqual(self._read_thread("cron01"), ("MiniMax-M3", "minimax"))
        # 普通 ChatGPT 老任务不动（留给后台全量迁移）
        self.assertEqual(self._read_thread("old02"), ("gpt-5-codex", "openai"))

    def test_full_background_pass_moves_old_openai_tasks(self):
        """后台全量迁移：window=None + include_openai 把 ChatGPT 老任务搬干净。"""
        from codex_switcher import engine
        path_a = self._make_rollout("task-old-1", "openai")
        path_b = self._make_rollout("task-old-2", "openai")
        self._make_db([
            self._thread_row("old01", "gpt-5-codex", "openai", path_a, age=30 * 86400),
            self._thread_row("old02", "gpt-5-codex", "openai", path_b, age=90 * 86400,
                             source="exec"),
        ])
        totals = engine.full_follow_all("openai", "minimax", "MiniMax-M3")
        self.assertEqual(totals["moved"], 2, totals)
        self.assertEqual(self._read_thread("old01"), ("MiniMax-M3", "minimax"))
        self.assertEqual(self._read_thread("old02"), ("MiniMax-M3", "minimax"))
        self.assertEqual(totals["exec_followed"], 1)

    def test_follow_back_to_openai_moves_exec_and_strips_third_party_history(self):
        """切回官方 OpenAI：定时任务照搬，第三方产生的垃圾条目同一次清掉。

        MiniMax 执行 web_search 产出的条目（只有 id 没有 call_id）回放给
        OpenAI 同样可能 400 —— 「切回去」不是免检通道。
        """
        from codex_switcher import threads
        path = self._make_dirty_rollout("task-back-openai", "minimax")
        self._make_db([self._thread_row("iii999", "MiniMax-M3", "minimax", path,
                                        age=7 * 86400, source="exec")])
        report = threads.follow_switch("minimax", "openai", "gpt-5-codex")
        self.assertEqual(report["moved"], 1, report)
        self.assertEqual(report["exec_followed"], 1)
        text = path.read_text()
        self.assertIn('"model_provider":"openai"', text)
        self.assertNotIn("web_search_call", text)
        self.assertNotIn("function_call_output", text)
        # 加密思考是 OpenAI 专有，第三方不会有；目标是 openai 时 reasoning 不误删
        self.assertEqual(report["items"][0].get("history"), 2, report["items"][0])

    def test_repair_skips_active_session_file(self):
        """看门狗每 12 秒跑一次 repair：Codex 正写着的文件绝不能改写。

        替换 inode 会让正在追加的写入丢失。数据库照样修（没有这个风险），
        会话文件留给后台在 Codex 退出后补上。
        """
        import time
        from codex_switcher import threads
        path = self._make_rollout("task-hot", "minimax")
        # mtime 保持最新（_make_rollout 把它拨到 1 小时前，这里拨回来）
        os.utime(path, (time.time(), time.time()))
        self._make_db([self._thread_row("jjj000", "deepseek-flash", "minimax", path)])
        report = threads.repair()
        self.assertEqual(report["fixed"], 1, report)
        # 数据库已对齐
        self.assertEqual(self._read_thread("jjj000"), ("deepseek-flash", "deepseek"))
        # 但文件原样未动：不是活动文件保护失效后那种被改写的样子
        self.assertIn('"model_provider": "minimax"', path.read_text())
        self.assertTrue(report["skipped_active"], report)


class IntegrationsTests(TempCodexHome):
    """平台全量能力环境：MCP 写入 config.toml、CLI 环境变量文件。"""

    @staticmethod
    def _record():
        return {"id": "minimax", "label": "MiniMax",
                "base_url": "https://api.minimax.cn/v1",
                "upstream_base_url": "https://api.minimax.cn/v1",
                "transport": "native"}

    def test_surface_lists_generation_and_mcp(self):
        from codex_switcher import capabilities as caps
        keys = {item["key"] for item in caps.platform_surface("minimax")}
        for expected in ("video", "image", "speech", "music", "web_search", "mcp", "cli"):
            self.assertIn(expected, keys)
        # 没收录的平台返回空列表，不编数据
        self.assertEqual(caps.platform_surface("no-such-platform"), [])
        # 平台 id 别名：用户用 glm/bigmodel 接入智谱时，能力面照样能查到
        self.assertEqual(len(caps.platform_surface("glm")),
                         len(caps.platform_surface("zhipu")))
        # MCP 项必须带可执行的配置，否则自动配置无从下手
        mcp = caps.platform_surface("minimax")
        mcp_config = next(i for i in mcp if i["key"] == "mcp")["mcp_config"]
        self.assertTrue(mcp_config.get("command"))
        self.assertTrue(mcp_config.get("env_key"))

    def test_sync_writes_mcp_block_and_env_file(self):
        from codex_switcher import integrations as integ
        result = integ.sync("minimax", self._record(), api_key="sk-test-123")
        self.assertIsNotNone(result["mcp"], result)
        self.assertEqual(result.get("errors") or [], [])
        env_path = Path(result["env_file"])
        self.assertTrue(env_path.exists())
        self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
        env_text = env_path.read_text()
        self.assertIn("MINIMAX_API_KEY", env_text)
        self.assertIn("sk-test-123", env_text)
        # MCP 表写进了 config.toml，且原有内容原样保留
        config_text = (self.home / "config.toml").read_text()
        document = load_document(config_text)
        block = (document.get("mcp_servers") or {}).get("minimax") or {}
        self.assertEqual(block.get("command"), "uvx")
        self.assertEqual((block.get("env") or {}).get("MINIMAX_API_KEY"), "sk-test-123")
        self.assertIn("[model_providers.existing]", config_text)
        self.assertIn("[mcp_servers.example]", config_text)

    def test_sync_is_idempotent(self):
        from codex_switcher import integrations as integ
        for _ in range(2):
            integ.sync("minimax", self._record(), api_key="sk-test-123")
        config_text = (self.home / "config.toml").read_text()
        self.assertEqual(config_text.count("[mcp_servers.minimax]"), 1)
        document = load_document(config_text)
        self.assertIn("minimax", document.get("mcp_servers") or {})

    def test_remove_cleans_mcp_and_env(self):
        from codex_switcher import integrations as integ
        result = integ.sync("minimax", self._record(), api_key="sk-test-123")
        env_path = Path(result["env_file"])
        integ.remove("minimax")
        self.assertFalse(env_path.exists())
        document = load_document((self.home / "config.toml").read_text())
        self.assertNotIn("minimax", document.get("mcp_servers") or {})
        # 别家的 MCP 不能被误伤
        self.assertIn("example", document.get("mcp_servers") or {})

    def test_status_reports_configuration_state(self):
        from codex_switcher import integrations as integ
        before = integ.status("minimax")
        self.assertTrue(before["available"])
        self.assertFalse(before["mcp"])
        integ.sync("minimax", self._record(), api_key="sk-test-123")
        after = integ.status("minimax")
        self.assertTrue(after["mcp"])
        self.assertIsNotNone(after["env_file"])

    def test_status_still_reports_mcp_without_a_toml_library(self):
        """3.9 / 3.10 上没装 tomli 时，不能因为「读不了配置文件」就谎报「没配置」。

        直接把 configfile.tomllib 置空来模拟缺失（而不是靠跳过整个用例），
        这样无论本机解释器有没有 tomllib，这条降级路径都被真实执行到。
        写入侧本来就带 toml_available() 守卫，读侧必须同样降级。
        """
        from codex_switcher import configfile as configfile_module
        from codex_switcher import integrations as integ
        integ.sync("minimax", self._record(), api_key="sk-test-123")
        saved = configfile_module.tomllib
        configfile_module.tomllib = None
        try:
            self.assertFalse(configfile_module.toml_available())
            configured = integ.status("minimax")
            # 没有 MCP 预设的平台在降级路径下也不能被误报成已配置
            absent = integ.status("deepseek")
        finally:
            configfile_module.tomllib = saved
        self.assertIs(configfile_module.tomllib, saved)
        self.assertTrue(configured["mcp"], configured)
        self.assertIsNotNone(configured["env_file"])
        self.assertFalse(absent["mcp"], absent)

    def test_provider_without_mcp_only_gets_env_file(self):
        from codex_switcher import integrations as integ
        record = dict(self._record(), id="deepseek")
        result = integ.sync("deepseek", record, api_key="sk-ds-1")
        self.assertIsNone(result["mcp"])
        self.assertIsNotNone(result["env_file"])
        # 环境文件里要有平台自己认的正式变量名（这里没有 -> 只写前缀变量）
        env_text = Path(result["env_file"]).read_text()
        self.assertIn("DEEPSEEK_API_KEY", env_text)
        self.assertIn("sk-ds-1", env_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
