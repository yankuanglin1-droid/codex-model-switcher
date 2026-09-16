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
        # 防呆：确认真的在临时目录里跑，而不是用户的家目录
        real_home = Path.home() / ".codex"
        self.assertNotEqual(Path(self._temp.name).resolve(), real_home.resolve())
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
