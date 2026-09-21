import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_switcher import readiness

class OfficialReadinessTests(unittest.TestCase):
    def check_config(self, text):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config.toml';path.write_text(text)
            with patch.object(readiness.paths,'config_path',return_value=path), patch.object(readiness,'_run_helper') as helper:
                result=readiness.check()
                helper.assert_not_called()
                return result

    def test_builtin_provider_requires_no_table(self):
        result=self.check_config('model_provider="openai"\nmodel="official-test"\n')
        self.assertTrue(result['ok']);self.assertFalse(result['network_verified'])

    def test_implicit_official_provider(self):
        self.assertTrue(self.check_config('model="official-test"\n')['ok'])

    def test_custom_official_override_warns(self):
        r=self.check_config('model_provider="openai"\n[model_providers.openai]\nbase_url="https://example.invalid"\n')
        self.assertTrue(any(c['name']=='官方配置覆盖' for c in r['checks']))

    def test_third_party_still_requires_table(self):
        self.assertFalse(self.check_config('model_provider="fixture-provider"\n')['ok'])

class WindowsProcessSafetyTests(unittest.TestCase):
    def test_another_python_is_not_the_switcher(self):
        from codex_switcher import platform_compat
        with patch.object(platform_compat,'IS_WINDOWS',True),patch.object(platform_compat,'process_command_line',return_value='python.exe unrelated.py'):
            self.assertFalse(platform_compat.is_our_process(45678))

    def test_switcher_command_is_recognized(self):
        from codex_switcher import platform_compat
        with patch.object(platform_compat,'IS_WINDOWS',True),patch.object(platform_compat,'process_command_line',return_value='python.exe -m codex_switcher app'):
            self.assertTrue(platform_compat.is_our_process(45678))

class SwitchBridgeTests(unittest.TestCase):
    def test_switch_starts_newly_required_bridge(self):
        from codex_switcher.webui import server
        from codex_switcher import cli,recovery
        with patch.object(server.engine,'switch_to',return_value={}),patch.object(server,'_state_payload',return_value={}),patch.object(cli,'_ensure_bridge') as ensure,patch.object(recovery,'status',return_value={'phase':'idle'}):
            server.Handler._dispatch(None,'switch',{'provider':'fixture','model':'test-model'})
            ensure.assert_called_once()

    def test_windows_portable_process_is_identified(self):
        from codex_switcher import platform_compat
        with patch.object(platform_compat,'IS_WINDOWS',True),patch.object(platform_compat,'process_command_line',return_value='pythonw.exe C:\\fixture\\packaging\\windows\\app.py'):
            self.assertTrue(platform_compat.is_our_process(45678))
