"""History safety regressions using synthetic files and no host/credential access."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from codex_switcher import engine, history, message_ids, threads


class HistoryPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="history-preservation-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def _rollout(self, name="rollout-fixture.jsonl"):
        path = self.home / name
        rows = [
            {"type": "session_meta", "payload": {"model_provider": "source"}},
            {"type": "response_item", "payload": {
                "type": "message", "id": "foreign-item", "role": "user",
                "content": [{"type": "input_text", "text": "synthetic message"}]}},
            {"type": "response_item", "payload": {
                "type": "function_call_output", "output": "synthetic orphan"}},
        ]
        path.write_bytes(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n")
        old = time.time() - 7200
        os.utime(path, (old, old))
        return path

    def _assert_writer_refused(self, writer, path):
        backup = self.home / (writer + "-backups")
        if writer == "message_ids":
            result = message_ids.repair_file(path, backup)
            self.assertEqual(result["changed"], 0)
            self.assertTrue(result.get("skipped"))
        elif writer == "sanitize":
            changed, _ = history.sanitize(path, False, backup)
            self.assertFalse(changed)
        else:
            with self.assertRaises(OSError):
                threads._rewrite_session_file(path, "source", "target", backup)

    def test_unknown_or_failed_probe_never_authorizes_old_files(self):
        path = self._rollout()
        with patch.object(history, "_codex_open_rollouts", return_value=None):
            self.assertTrue(history.busy_reason(path))
            result = message_ids.repair_file(path, self.home / "backups", host_closed=True)
            self.assertEqual(result["changed"], 0)
            self.assertTrue(result.get("skipped"))

        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                completed = subprocess.CompletedProcess(
                    ["lsof"], returncode, stdout=b"", stderr=b"lsof: permission denied\n")
                with patch.dict(history._OPEN_FILES_CACHE, {
                    "at": 0.0, "paths": history._UNPROBED, "broken": False}, clear=True), \
                        patch.object(history.subprocess, "run", return_value=completed):
                    self.assertTrue(history.busy_reason(path))

    def test_writers_abort_when_final_occupancy_becomes_unknown(self):
        real_mkstemp = tempfile.mkstemp
        for writer in ("message_ids", "sanitize", "threads"):
            with self.subTest(writer=writer):
                path = self._rollout(writer + ".jsonl")
                original, original_inode = path.read_bytes(), path.stat().st_ino
                phase = {"temporary_created": False}

                def create_temporary(*args, **kwargs):
                    result = real_mkstemp(*args, **kwargs)
                    phase["temporary_created"] = True
                    return result

                def occupancy(_):
                    return "occupancy-unknown" if phase["temporary_created"] else None

                with patch.object(history, "busy_reason", side_effect=occupancy), \
                        patch.object(tempfile, "mkstemp", side_effect=create_temporary), \
                        patch.object(os, "replace", wraps=os.replace) as replace:
                    self._assert_writer_refused(writer, path)
                    replace.assert_not_called()
                self.assertTrue(phase["temporary_created"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_ino, original_inode)

    def test_writers_abort_if_path_changes_inode_with_identical_content(self):
        real_mkstemp, real_replace = tempfile.mkstemp, os.replace
        for writer in ("message_ids", "sanitize", "threads"):
            with self.subTest(writer=writer):
                path = self._rollout(writer + ".jsonl")
                original, before = path.read_bytes(), path.stat()
                replacement = self.home / (writer + "-replacement.jsonl")
                replacement.write_bytes(original)
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement_inode = replacement.stat().st_ino
                self.assertNotEqual(replacement_inode, before.st_ino)

                def create_temporary(*args, **kwargs):
                    result = real_mkstemp(*args, **kwargs)
                    # Mimic another process replacing the path after our snapshot.
                    real_replace(replacement, path)
                    return result

                with patch.object(history, "busy_reason", return_value=None), \
                        patch.object(tempfile, "mkstemp", side_effect=create_temporary), \
                        patch.object(os, "replace", wraps=real_replace) as replace:
                    self._assert_writer_refused(writer, path)
                    replace.assert_not_called()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_ino, replacement_inode)

    def test_default_switches_preserve_history_and_database_files(self):
        from codex_switcher import integrations

        rollout = self._rollout()
        state_db = self.home / "state_5.sqlite"
        catalog_db = self.home / "sqlite" / "codex-dev.db"
        catalog_db.parent.mkdir()
        state_db.write_bytes(b"synthetic state database sentinel")
        catalog_db.write_bytes(b"synthetic catalog database sentinel")
        snapshots = {path: (path.read_bytes(), path.stat().st_ino)
                     for path in (rollout, state_db, catalog_db)}
        config = self.home / "config.toml"
        config.write_text('model_provider = "source"\nmodel = "old-model"\n')
        record = engine.build_provider_record(
            provider_id="target", label="Fixture", base_url="https://example.invalid/v1",
            models_url="https://example.invalid/models", transport="native", requires_key=False)
        record["models"] = {"fixture-model": {}}
        state = {"schema_version": 3, "providers": {"target": record}}

        with patch.object(engine.state_module, "load", return_value=state), \
                patch.object(engine.state_module, "save"), \
                patch.object(engine.secrets, "load", side_effect=AssertionError("credential access")), \
                patch.object(integrations, "sync", return_value={}), \
                patch.object(threads, "follow_switch") as follow, \
                patch.object(threads, "repair") as repair, \
                patch.object(threads, "_update_databases") as update_databases, \
                patch.object(history, "auto_clean") as clean, \
                patch.object(history, "sweep_all") as sweep, \
                patch.object(engine, "schedule_full_follow") as schedule_follow, \
                patch.object(engine, "schedule_history_sweep") as schedule_sweep:
            for provider, model in (("target", "fixture-model"), ("openai", "gpt-5-codex")):
                with self.subTest(provider=provider):
                    result = engine.switch_to(provider, model)
                    self.assertTrue(result["history_preserved"])
                    for path, (original, inode) in snapshots.items():
                        self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(path.stat().st_ino, inode)
            for operation in (follow, repair, update_databases, clean, sweep,
                              schedule_follow, schedule_sweep):
                operation.assert_not_called()

    def test_server_startup_and_watchdog_only_run_dry_checks(self):
        from codex_switcher import recovery
        from codex_switcher.webui import server

        http_server = MagicMock()
        http_server.server_address = ("127.0.0.1", 18000)
        # Execute the scheduler's job synchronously without starting a real thread.
        def synchronous_thread(*args, **kwargs):
            return SimpleNamespace(start=kwargs["target"])

        with patch.object(server, "ThreadingHTTPServer", return_value=http_server), \
                patch.object(server, "_write_runtime_state"), \
                patch.object(server, "_clear_runtime_state"), \
                patch.object(server, "_start_watchdog"), \
                patch.object(server.Handler, "token", "fixture-token", create=True), \
                patch.object(engine, "ALLOW_BACKGROUND_FOLLOW", True), \
                patch.object(engine.threading, "Thread", side_effect=synchronous_thread), \
                patch.object(recovery, "status", return_value={"phase": "idle"}), \
                patch.object(threads, "repair", return_value={"fixed": 0}) as repair, \
                patch.object(history, "sweep_all", return_value={"cleaned": 0}) as sweep, \
                patch("builtins.print"):
            self.assertEqual(server.run(open_browser=False, token="fixture-token"), 0)
            with patch.object(server.time, "sleep", side_effect=[None] * 5 + [RuntimeError("stop")]):
                server._watchdog_loop(interval=0)
            self.assertEqual(repair.call_count, 6)
            self.assertEqual(sweep.call_count, 2)
            for operation in (repair, sweep):
                for call in operation.call_args_list:
                    self.assertIs(call.kwargs.get("dry_run"), True)

    def test_skipped_or_failed_sessions_never_update_databases(self):
        state = {"providers": {"target": {"models": {"fixture-model": {}}}}}
        for operation in ("repair", "follow_switch"):
            for reason in ("missing", "unknown", "rewrite-error"):
                with self.subTest(operation=operation, reason=reason):
                    path = self._rollout(operation + "-" + reason + ".jsonl")
                    if reason == "missing":
                        path.unlink()
                    item = {"id": "fixture-thread", "title": "Fixture", "model": "fixture-model",
                            "provider": "source", "rollout_path": str(path),
                            "updated_at": time.time(), "source": "cli"}
                    with patch.object(engine.state_module, "load", return_value=state), \
                            patch.object(threads, "list_threads", return_value=[item]), \
                            patch.object(history, "busy_reason", return_value=(
                                "occupancy-unknown" if reason == "unknown" else None)), \
                            patch.object(threads, "_rewrite_session_file", side_effect=OSError("fixture failure")), \
                            patch.object(threads, "_update_databases") as update_databases:
                        if operation == "repair":
                            result = threads.repair()
                            self.assertEqual(result["fixed"], 0)
                        else:
                            result = threads.follow_switch("source", "target", "fixture-model")
                            self.assertEqual(result["moved"], 0)
                        update_databases.assert_not_called()


if __name__ == "__main__":
    unittest.main()
