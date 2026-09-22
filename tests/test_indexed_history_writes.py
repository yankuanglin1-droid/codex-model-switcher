"""Synthetic indexed history: source commits must invalidate only replay cursors."""

from contextlib import contextmanager
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_switcher import codexapp, history, history_write_guard, message_ids, projection, threads


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class IndexedHistoryWrites(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="indexed-history-writes-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.backups = self.home / "backups"
        self.state_db = self.home / "state_5.sqlite"
        self.history_db = self.home / "thread_history_1.sqlite"
        for context in (patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
                        patch.object(history, "busy_reason", return_value=None),
                        patch.object(history_write_guard.sys, "platform", "darwin"),
                        patch.object(codexapp, "assert_history_idle"),
                        patch.object(codexapp, "is_running", return_value=False)):
            context.start()
            self.addCleanup(context.stop)
        with database(self.state_db) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER)")
        with database(self.history_db) as db:
            db.execute("CREATE TABLE thread_history_projection_state("
                       "thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)")
            for table in ("thread_items", "turns", "realtime"):
                db.execute("CREATE TABLE " + table + "(thread_id TEXT, payload TEXT)")
                db.executemany("INSERT INTO " + table + " VALUES (?,?)",
                               [(name, "synthetic " + table) for name in ("target", "other", "archived")])
        self.path, self.original = self.make_source("target")
        self.make_source("other")
        self.make_source("archived", archived=True)

    def make_source(self, thread_id, archived=False):
        path = self.home / (thread_id + ".jsonl")
        rows = [
            {"type": "session_meta", "payload": {"id": thread_id, "model_provider": "source"}},
            {"type": "response_item", "payload": {"type": "message", "id": "foreign-message",
             "role": "user", "content": [{"type": "input_text", "text": "synthetic user text"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "id": "fc_fixture",
             "call_id": "pair", "name": "fixture", "arguments": '{"raw":"preserve"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "pair",
             "output": [{"type": "input_image", "image_url": "data:synthetic"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_fixture",
             "content": [{"type": "reasoning_text", "text": "synthetic"}], "encrypted_content": "ciphertext"}},
        ]
        raw = b"\r\n" + b"\r\n\r\n".join(json.dumps(row).encode() for row in rows)
        path.write_bytes(raw)
        with database(self.state_db) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?)", (thread_id, str(path), int(archived)))
        with database(self.history_db) as db:
            db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", (thread_id, len(raw) + 100, 5))
        return path, raw

    def cursor_ids(self):
        with database(self.history_db) as db:
            return [row[0] for row in db.execute("SELECT thread_id FROM thread_history_projection_state ORDER BY thread_id")]

    def cursor(self, thread_id):
        with database(self.history_db) as db:
            return db.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM "
                              "thread_history_projection_state WHERE thread_id=?", (thread_id,)).fetchone()

    def displayed(self):
        with database(self.history_db) as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY thread_id").fetchall()
                    for table in ("thread_items", "turns", "realtime")}

    def write(self, writer, path=None):
        path = path or self.path
        if writer == "ids":
            return message_ids.repair_file(path, self.backups, host_closed=True)
        return threads._rewrite_session_file(path, "source", "target", self.backups, host_closed=True)

    def test_cursor_reset_before_each_source_replace_and_displayed_rows_survive(self):
        for writer in ("ids", "provider"):
            with self.subTest(writer=writer):
                path, original = self.make_source("target-" + writer)
                displayed = self.displayed()
                real_replace = os.replace

                def replace(source, target):
                    self.assertEqual(Path(target), path)
                    self.assertEqual(self.cursor("target-" + writer), (0, 0))
                    self.assertIn("other", self.cursor_ids())
                    self.assertIn("archived", self.cursor_ids())
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(self.displayed(), displayed)
                    self.assertTrue(any(p.read_bytes() == original for p in self.backups.iterdir()))
                    return real_replace(source, target)

                with patch.object(os, "replace", side_effect=replace) as commit:
                    result = self.write(writer, path)
                commit.assert_called_once()
                self.assertTrue(result["changed"] if writer == "ids" else result[0])
                self.assertEqual(self.displayed(), displayed)
                before = [json.loads(line) for line in original.splitlines() if line.strip()]
                after = [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
                if writer == "ids":
                    after[1]["payload"]["id"] = before[1]["payload"]["id"]
                else:
                    after[0]["payload"]["model_provider"] = before[0]["payload"]["model_provider"]
                self.assertEqual(after, before)
                # Exact original line endings and untouched record bytes survive.
                self.assertTrue(path.read_bytes().startswith(b"\r\n"))
                self.assertEqual(path.read_bytes().count(b"\r\n"), original.count(b"\r\n"))
                self.assertFalse(path.read_bytes().endswith(b"\n"))
                self.assertIn(original.splitlines(keepends=True)[-1], path.read_bytes())

    def test_running_or_unverifiable_host_refuses_all_indexed_writers(self):
        for writer in ("ids", "provider"):
            for status in (True, RuntimeError("synthetic probe failure")):
                with self.subTest(writer=writer, status=type(status).__name__):
                    kwargs = {"side_effect": status} if isinstance(status, Exception) else {"return_value": status}
                    with patch.object(codexapp, "is_running", **kwargs), patch.object(os, "replace") as replace:
                        with self.assertRaises(OSError):
                            self.write(writer)
                        replace.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), self.original)
                    self.assertIn("target", self.cursor_ids())

    def test_noop_and_dry_run_never_invalidate_a_live_hosts_cursor(self):
        with patch.object(codexapp, "is_running", return_value=True), \
                patch.object(projection, "invalidate_projection") as invalidate:
            report = message_ids.repair_file(self.path, self.backups, dry_run=True)
            self.assertEqual(report["would_change"], 1)
            self.assertEqual(threads._rewrite_session_file(
                self.path, "absent", "target", self.backups), (False, 0, 0, 0))
            invalidate.assert_not_called()
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_host_restarted_during_invalidation_cannot_commit_source(self):
        for writer in ("ids", "provider"):
            path, original = self.make_source("restarted-" + writer)
            with self.subTest(writer=writer), \
                    patch.object(codexapp, "is_running", side_effect=[False, True]), \
                    patch.object(os, "replace") as replace:
                with self.assertRaises(OSError):
                    self.write(writer, path)
                replace.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.cursor("restarted-" + writer), (0, 0))

    def test_archived_sources_are_refused_even_without_projection_database(self):
        path = self.home / "archived.jsonl"
        original = path.read_bytes()
        for indexed in (True, False):
            if not indexed:
                self.history_db.unlink()
            for writer in ("ids", "provider"):
                with self.subTest(indexed=indexed, writer=writer), patch.object(os, "replace") as replace:
                    with self.assertRaises(OSError):
                        self.write(writer, path)
                    replace.assert_not_called()
                    self.assertEqual(path.read_bytes(), original)

    def test_same_id_at_another_path_cannot_invalidate_the_active_source(self):
        clone = self.home / "stale-copy.jsonl"
        clone.write_bytes(self.original)
        with self.assertRaises(OSError):
            self.write("ids", clone)
        self.assertIn("target", self.cursor_ids())
        self.assertEqual(clone.read_bytes(), self.original)

    def test_invalid_invalidation_receipts_cannot_authorize_replacement(self):
        reports = [None, {}, {"invalidated": 0, "receipts": [], "skipped": []},
                   {"error": "synthetic", "invalidated": 1},
                   {"invalidated": 0, "receipts": [], "skipped": [{"thread_id": "target",
                    "rollout_path": str(self.path), "reason": "valid_cursor"}]}]
        for writer in ("ids", "provider"):
            for report in reports:
                with self.subTest(writer=writer, report=report), \
                        patch.object(projection, "invalidate_projection", return_value=report), \
                        patch.object(os, "replace") as replace:
                    with self.assertRaises(OSError):
                        self.write(writer)
                    replace.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_append_after_invalidation_preserves_the_append(self):
        real_invalidate = projection.invalidate_projection
        for writer in ("ids", "provider"):
            path, original = self.make_source("concurrent-" + writer)
            appended = b'\n{"type":"event_msg","payload":{"type":"synthetic"}}\n'

            def invalidate(*args, **kwargs):
                report = real_invalidate(*args, **kwargs)
                with path.open("ab") as stream:
                    stream.write(appended)
                return report

            with self.subTest(writer=writer), patch.object(projection, "invalidate_projection", side_effect=invalidate), \
                    patch.object(os, "replace") as replace:
                if writer == "ids":
                    self.assertEqual(self.write(writer, path)["skipped"], "concurrent-write")
                else:
                    with self.assertRaises(OSError):
                        self.write(writer, path)
                replace.assert_not_called()
            self.assertEqual(path.read_bytes(), original + appended)
            self.assertEqual(self.cursor("concurrent-" + writer), (0, 0))

    def test_interruption_after_invalidation_leaves_original_replayable(self):
        for writer in ("ids", "provider"):
            path, original = self.make_source("interrupted-" + writer)
            with self.subTest(writer=writer), patch.object(os, "replace", side_effect=OSError("synthetic interruption")):
                with self.assertRaises(OSError):
                    self.write(writer, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(self.cursor("interrupted-" + writer), (0, 0))
            self.assertTrue(any(p.read_bytes() == original for p in self.backups.iterdir()))

    def test_missing_cursor_and_legacy_state_membership_are_safe_to_repair(self):
        with database(self.history_db) as db:
            db.execute("DELETE FROM thread_history_projection_state WHERE thread_id='target'")
        self.assertEqual(self.write("ids")["changed"], 1)
        self.history_db.unlink()
        self.assertTrue(self.write("provider")[0])

    def test_already_reset_cursor_is_a_verified_idempotent_write_guard(self):
        self.assertEqual(self.write("ids")["changed"], 1)
        self.assertEqual(self.cursor("target"), (0, 0))
        self.assertTrue(self.write("provider")[0])
        self.assertEqual(self.cursor("target"), (0, 0))

    def test_raw_repair_undo_is_durable_private_and_restores_only_owned_bytes(self):
        displayed = self.displayed()
        result = self.write("ids")
        receipt = Path(result["undo_receipt"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic user text", receipt.read_text())
        self.assertNotEqual(self.path.read_bytes(), self.original)
        self.assertEqual(message_ids.rollback_repair(receipt, host_closed=True)["status"], "restored")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.displayed(), displayed)
        self.assertEqual(self.cursor("target"), (0, 0))
        self.assertEqual(message_ids.rollback_repair(receipt, host_closed=True)["status"], "already-restored")

    def test_raw_replace_then_exception_recovers_original_and_keeps_primary_error(self):
        replace = os.replace
        def interrupted(source, target):
            replace(source, target)
            if Path(source).name.startswith(".message-ids-"):
                raise OSError("synthetic failure after replacement")
        with patch.object(os, "replace", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "synthetic failure") as caught:
                self.write("ids")
        self.assertEqual(caught.exception._history_rollback["status"], "restored")
        self.assertTrue(Path(caught.exception._history_undo_receipt).is_file())
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.cursor("target"), (0, 0))

    def test_raw_rollback_never_overwrites_concurrent_append(self):
        result = self.write("ids")
        with self.path.open("ab") as stream:
            stream.write(b'\n{"type":"concurrent-record"}\n')
        concurrent = self.path.read_bytes()
        with self.assertRaisesRegex(OSError, "rollback deferred"):
            message_ids.rollback_repair(result["undo_receipt"], host_closed=True)
        self.assertEqual(self.path.read_bytes(), concurrent)

    def test_raw_rollback_never_overwrites_replaced_inode_even_with_same_bytes(self):
        result = self.write("ids")
        other = self.home / "same-bytes.jsonl"
        current = self.path.read_bytes()
        other.write_bytes(current)
        os.replace(other, self.path)
        with self.assertRaisesRegex(OSError, "rollback deferred"):
            message_ids.rollback_repair(result["undo_receipt"], host_closed=True)
        self.assertEqual(self.path.read_bytes(), current)

    def test_raw_rollback_refuses_changed_backup(self):
        result = self.write("ids")
        current = self.path.read_bytes()
        Path(result["backup"]).write_bytes(b"untrusted changed backup")
        with self.assertRaisesRegex(OSError, "backup changed"):
            message_ids.rollback_repair(result["undo_receipt"], host_closed=True)
        self.assertEqual(self.path.read_bytes(), current)

    def test_raw_rollback_refuses_a_reopened_host(self):
        result = self.write("ids")
        current = self.path.read_bytes()
        with patch.object(codexapp, "is_running", return_value=True):
            with self.assertRaisesRegex(OSError, "Close ChatGPT/Codex"):
                message_ids.rollback_repair(result["undo_receipt"], host_closed=True)
        self.assertEqual(self.path.read_bytes(), current)

    def test_legacy_sanitize_retains_every_byte_and_reports_refusal(self):
        original_displayed, original_cursors = self.displayed(), self.cursor_ids()
        for moving, cross in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(moving=moving, cross=cross), \
                    patch.object(projection, "invalidate_projection") as invalidate, \
                    patch.object(os, "replace") as replace:
                changed, stats = history.sanitize(self.path, moving, self.backups, cross_provider=cross)
                self.assertFalse(changed)
                self.assertEqual(stats["skipped"], "destructive-cleanup-disabled")
                self.assertEqual(stats["removed_image_outputs"], 0)
                self.assertEqual(stats["diagnostics"]["image_outputs"], 1)
                invalidate.assert_not_called()
                replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.displayed(), original_displayed)
        self.assertEqual(self.cursor_ids(), original_cursors)
        self.assertFalse(self.backups.exists())

    def test_plaintext_only_official_file_repair_aborts_without_backups_or_invalidation(self):
        records = [json.loads(line) for line in self.original.splitlines() if line.strip()]
        del records[-1]["payload"]["encrypted_content"]
        original = b"\n".join(json.dumps(record).encode() for record in records)
        self.path.write_bytes(original)
        with patch.object(projection, "invalidate_projection") as invalidate, \
                patch.object(os, "replace") as replace:
            with self.assertRaises(ValueError):
                message_ids.repair_file(self.path, self.backups, host_closed=True, official=True)
            invalidate.assert_not_called()
            replace.assert_not_called()
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.path.read_bytes(), original)


class OfficialReasoningSafety(unittest.TestCase):
    def test_plaintext_reasoning_rejected_before_mutating_any_item(self):
        for encrypted in (None, "", " ", [], {}, 1):
            record = {"type": "compacted", "payload": {"replacement_history": [
                {"type": "message", "id": "foreign", "content": []},
                {"type": "reasoning", "id": "foreign-reasoning", "content": [{"text": "synthetic"}],
                 "encrypted_content": encrypted}]}}
            before = copy.deepcopy(record)
            with self.subTest(encrypted=encrypted), self.assertRaises(ValueError):
                message_ids.normalize_record(record, official=True)
            self.assertEqual(record, before)

    def test_only_nonempty_content_arrays_with_ciphertext_can_be_removed(self):
        for content in (None, [], "synthetic unexpected shape", {"text": "synthetic"}):
            record = {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_fixture",
                      "content": content, "encrypted_content": "synthetic ciphertext", "summary": []}}
            before = copy.deepcopy(record)
            self.assertEqual(message_ids.normalize_record(record, official=True), 0)
            self.assertEqual(record, before)


if __name__ == "__main__":
    unittest.main()
