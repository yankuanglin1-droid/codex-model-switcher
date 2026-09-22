"""Context configuration regressions using synthetic providers in a temporary home."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switcher import catalog, configfile, contextguard, engine, paths, state


@unittest.skipUnless(configfile.toml_available(), "Requires tomllib or tomli")
class ContextSettingsSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="switcher-context-safety-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        integrations = patch("codex_switcher.integrations.sync", return_value={})
        integrations.start()
        self.addCleanup(integrations.stop)

    def write_config(self, provider_id, model_id, window):
        paths.config_path().write_text(
            'model_provider = "%s"\n'
            'model = "%s"\n'
            'model_context_window = %d\n'
            'model_auto_compact_token_limit = %d\n'
            'service_tier = "priority"\n'
            'model_verbosity = "medium"\n'
            'approval_policy = "never"\n'
            '\n[desktop]\n'
            'appearanceTheme = "dark"\n'
            % (provider_id, model_id, window, int(window * 0.57)),
            encoding="utf-8",
        )

    def write_provider(self, provider_id, model_id, window, override=False):
        record = {
            "id": provider_id,
            "label": "Synthetic provider",
            "base_url": "https://example.invalid/v1",
            "transport": "native",
            "requires_key": False,
            "models": {model_id: {}},
            "default_model": model_id,
            "model_overrides": {model_id: {"context_window": window}} if override else {},
        }
        document = state.default_state()
        state.upsert_provider(document, record)
        state.save(document)
        target = catalog.catalog_path(provider_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"models": [{
            "slug": model_id,
            "context_window": window,
            "effective_context_window_percent": catalog.DEFAULT_EFFECTIVE_PERCENT,
        }]}), encoding="utf-8")
        return record

    def assert_preferences_preserved(self, document):
        self.assertEqual(document["service_tier"], "priority")
        self.assertEqual(document["model_verbosity"], "medium")
        self.assertEqual(document["approval_policy"], "never")
        self.assertEqual(document["desktop"], {"appearanceTheme": "dark"})

    def test_missing_catalog_uses_explicit_context_override(self):
        record = {
            "id": "synthetic-provider",
            "model_overrides": {"synthetic-model": {"context_window": 100000}},
        }
        self.assertFalse(catalog.catalog_path(record["id"]).exists())
        settings = engine.provider_settings(record, "synthetic-model")
        effective = contextguard.effective_window({
            "context_window": 100000,
            "effective_context_window_percent": catalog.DEFAULT_EFFECTIVE_PERCENT,
        })
        self.assertEqual(settings["model_context_window"], 100000)
        self.assertEqual(settings["model_auto_compact_token_limit"],
                         contextguard.auto_compact_limit(effective))

    def test_switch_between_large_and_small_catalogs_discards_source_override(self):
        for source_window, target_window in [(1000000, 100000), (100000, 1000000)]:
            with self.subTest(source_window=source_window, target_window=target_window):
                self.write_config("synthetic-source", "source-model", source_window)
                self.write_provider("synthetic-target", "target-model", target_window)
                engine.switch_to("synthetic-target", "target-model")
                document = configfile.parse(paths.config_path().read_text())
                self.assertNotIn("model_context_window", document)
                self.assertEqual(document["model_provider"], "synthetic-target")
                self.assertEqual(document["model"], "target-model")
                self.assertEqual(document["model_catalog_json"],
                                 str(catalog.catalog_path("synthetic-target")))
                effective = contextguard.window_for_model(
                    document["model_catalog_json"], "target-model")
                self.assertEqual(document["model_auto_compact_token_limit"],
                                 contextguard.auto_compact_limit(effective))
                self.assert_preferences_preserved(document)

    def test_switch_keeps_explicit_target_override(self):
        self.write_config("synthetic-source", "source-model", 1000000)
        self.write_provider("synthetic-target", "target-model", 100000, override=True)
        engine.switch_to("synthetic-target", "target-model")
        document = configfile.parse(paths.config_path().read_text())
        self.assertEqual(document["model_context_window"], 100000)
        self.assert_preferences_preserved(document)

    def test_refresh_current_model_removes_withdrawn_override(self):
        self.write_config("synthetic-target", "target-model", 1000000)
        record = self.write_provider("synthetic-target", "target-model", 100000)
        self.assertTrue(engine._rewrite_if_current(record, "target-model"))
        document = configfile.parse(paths.config_path().read_text())
        self.assertNotIn("model_context_window", document)
        self.assert_preferences_preserved(document)


if __name__ == "__main__":
    unittest.main()
