"""Recovery must not inherit another desktop task's IPC or credentials."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_switcher import history_rebuild


@unittest.skipUnless(os.name == "posix", "reader uses owned POSIX groups")
class RecoveryReaderEnvironmentTests(unittest.TestCase):
    def test_only_isolated_codex_home_survives_host_environment(self):
        values = {
            "CODEX_HOME": "/fixture/real-home",
            "CODEX_SQLITE_HOME": "/fixture/real-database",
            "CODEX_APP_TOOLS_PIPE_PATH": "/fixture/host-pipe",
            "CODEX_THREAD_ID": "fixture-thread",
            "CODEX_SESSION_ID": "fixture-session",
            "CODEX_PERMISSION_PROFILE": "fixture-permissions",
            "OPENAI_API_KEY": "synthetic-test-key",
            "FIXTURE_AUTH_TOKEN": "synthetic-test-token",
            "PATH": "/usr/bin:/bin",
        }
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, values, clear=True), \
                patch.object(history_rebuild.subprocess, "Popen", side_effect=OSError("synthetic stop")) as launch:
            with self.assertRaisesRegex(OSError, "synthetic stop"):
                history_rebuild.LocalReader("/fixture/codex", folder)
            self.assertEqual(launch.call_args.kwargs["env"], {
                "PATH": "/usr/bin:/bin", "CODEX_HOME": str(Path(folder).resolve())})
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
