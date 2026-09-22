"""Projection safety tests use only disposable synthetic state and rollouts."""
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codex_switcher import projection


@contextmanager
def _database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="projection-fixture-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "state_10.sqlite"
        self.history = self.home / "thread_history_2.sqlite"
        with _database(self.state) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
                               "archived INTEGER NOT NULL, history_mode TEXT, title TEXT)")
        self._history_db(self.history)

    def _history_db(self, path):
        with _database(path) as connection:
            connection.execute("CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY, "
                               "next_rollout_byte_offset INTEGER NOT NULL, next_rollout_ordinal INTEGER NOT NULL)")
            for table in ("thread_history_turns", "thread_history_items", "thread_realtime_items"):
                connection.execute("CREATE TABLE " + table + " (thread_id TEXT, payload TEXT)")
                connection.execute("INSERT INTO " + table + " VALUES (?,?)", ("active", "private synthetic body"))
            connection.execute(projection.KNOWN_DELETE_CLEANUP_SQL)

    def _thread(self, thread_id="active", *, archived=False, offset=99999, ordinal=4):
        source = self.home / ("rollout-" + thread_id + ".jsonl")
        header = json.dumps({"type": "session_meta", "payload": {"id": thread_id}}).encode() + b"\n"
        source.write_bytes(header + b'{"type":"response_item","payload":{"text":"private synthetic body"}}\n')
        with _database(self.state) as connection:
            connection.execute("INSERT INTO threads VALUES (?,?,?,?,?)",
                               (thread_id, str(source), int(archived), "persisted", "private synthetic title"))
        if offset is not None:
            with _database(self.history) as connection:
                connection.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)",
                                   (thread_id, offset, ordinal))
        return source, len(header)

    def _rows(self, database=None, table="thread_history_projection_state"):
        with _database(database or self.history) as connection:
            return connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()

    def _offset(self, offset, thread_id="active"):
        with _database(self.history) as connection:
            connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?",
                               (offset, thread_id))

    def _snapshot(self):
        return {p.relative_to(self.home): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}

    def test_audit_is_readonly_and_uses_numeric_versions(self):
        source, _ = self._thread()
        archived, _ = self._thread("archived", archived=True)
        archived.unlink()  # Archived source must never be inspected.
        lower = self.home / "state_9.sqlite"
        lower.write_bytes(b"ignored old database")
        second = self.home / "thread_history_11.sqlite"
        self._history_db(second)
        before = self._snapshot()
        actual_connect = sqlite3.connect
        with patch.object(projection.sqlite3, "connect", wraps=actual_connect) as connect:
            report = projection.audit_projection(self.home)
        self.assertEqual(report["state_db"], str(self.state))
        self.assertEqual(report["history_dbs"], [str(self.history), str(second)])
        self.assertEqual(report["active_threads"], 1)
        self.assertEqual(report["issues"], [])
        self.assertEqual({e["thread_id"] for e in report["entries"]}, {"active"})
        self.assertEqual([e["status"] for e in report["entries"]], ["beyond_eof", "missing_cursor"])
        for call in connect.call_args_list:
            self.assertIn("?mode=ro", call.args[0])
            self.assertIs(call.kwargs["uri"], True)
        self.assertEqual(before, self._snapshot())
        self.assertNotIn("private synthetic", json.dumps(report))

    def test_byte_boundaries_include_zero_newline_and_eof(self):
        source, first_boundary = self._thread()
        data = source.read_bytes()
        source.write_bytes(data + '{"text":"你好"}\r\n'.encode())
        size = source.stat().st_size
        for offset, status in ((-1, "negative_offset"), (0, "valid"), (first_boundary, "valid"),
                               (first_boundary + 1, "not_line_boundary"), (size - 1, "not_line_boundary"),
                               (size, "valid"), (size + 1, "beyond_eof")):
            with self.subTest(offset=offset):
                self._offset(offset)
                result = projection.audit_projection(self.home)
                self.assertEqual(result["entries"][0]["status"], status)
        source.write_bytes(source.read_bytes().rstrip(b"\r\n"))
        self._offset(source.stat().st_size)
        self.assertEqual(projection.audit_projection(self.home)["entries"][0]["status"], "not_line_boundary")

    def test_missing_and_unreadable_sources_are_reported_and_refused(self):
        source, _ = self._thread()
        original_open = Path.open
        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError("synthetic refusal")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", denied):
            self.assertEqual(projection.audit_projection(self.home)["entries"][0]["status"], "unreadable_source")
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["active"], host_closed=True)
            self.assertEqual(error.exception.code, "unreadable_source")
        source.unlink()
        self.assertEqual(projection.audit_projection(self.home)["entries"][0]["status"], "missing_source")
        with self.assertRaises(projection.ProjectionError):
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(len(self._rows()), 1)
        self.assertFalse((self.home / "projection-backups").exists())

    def test_archived_and_unknown_targets_never_change_any_rows(self):
        self._thread()
        self._thread("archived", archived=True)
        before = self._snapshot()
        for targets in (["archived"], ["active", "archived"], ["absent"]):
            with self.subTest(targets=targets), self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, targets, host_closed=True, force=True)
            self.assertEqual(error.exception.code, "inactive_thread")
            self.assertEqual(before, self._snapshot())

    def test_host_closed_is_required_and_literal_true(self):
        self._thread()
        before = self._snapshot()
        for value in (False, None, 1, "true"):
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["active"], host_closed=value)
            self.assertEqual(error.exception.code, "host_not_closed")
        self.assertEqual(before, self._snapshot())

    def test_invalidation_preserves_source_and_all_displayed_tables(self):
        source, _ = self._thread()
        self._thread("archived", archived=True)
        source_bytes = source.read_bytes()
        content = {table: self._rows(table=table) for table in
                   ("thread_history_turns", "thread_history_items", "thread_realtime_items")}
        with _database(self.history) as connection:
            trigger_before = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        result = projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(result["invalidated"], 1)
        self.assertEqual(self._rows(), [("active", 0, 0), ("archived", 99999, 4)])
        self.assertEqual(source.read_bytes(), source_bytes)
        for table, rows in content.items():
            self.assertEqual(self._rows(table=table), rows)
        with _database(self.history) as connection:
            self.assertEqual(connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall(), trigger_before)
        backup = Path(result["backup"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        payload = json.loads(backup.read_text())
        self.assertEqual(payload["columns"], list(projection.CURSOR_FIELDS))
        self.assertEqual(payload["rows"], result["receipts"])
        self.assertEqual(payload["rows"][0]["next_rollout_ordinal"], 4)
        self.assertEqual(payload["rows"][0]["next_rollout_byte_offset"], 99999)
        self.assertEqual(payload["rows"][0]["operation"], "reset_cursor")
        self.assertEqual(payload["rows"][0]["new_next_rollout_byte_offset"], 0)
        self.assertEqual(payload["rows"][0]["new_next_rollout_ordinal"], 0)
        self.assertNotIn("private synthetic", backup.read_text())

    def test_force_and_idempotence(self):
        source, boundary = self._thread(offset=0)
        result = projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(result["invalidated"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "valid_cursor")
        self.assertIsNone(result["backup"])
        result = projection.invalidate_projection(self.home, ["active"], host_closed=True, force=True,
                                                  rollout_paths={"active": source})
        self.assertEqual(result["invalidated"], 1)
        backups = list((self.home / "projection-backups").iterdir())
        repeat = projection.invalidate_projection(self.home, ["active"], host_closed=True, force=True)
        self.assertEqual(repeat["invalidated"], 0)
        self.assertEqual(repeat["skipped"][0]["reason"], "already_reset")
        self.assertEqual(self._rows(), [("active", 0, 0)])
        self.assertIsNone(repeat["backup"])
        self.assertEqual(backups, list((self.home / "projection-backups").iterdir()))

    def test_missing_cursor_remains_absent(self):
        self._thread(offset=None)
        result = projection.invalidate_projection(self.home, ["active"], host_closed=True, force=True)
        self.assertEqual(result["invalidated"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "missing_cursor")
        self.assertEqual(self._rows(), [])
        self.assertIsNone(result["backup"])

    def test_no_index_still_validates_state_and_source(self):
        source, _ = self._thread()
        self.history.unlink()
        result = projection.invalidate_projection(self.home, ["active"], host_closed=True, force=True)
        self.assertEqual(result["invalidated"], 0)
        self.assertEqual(result["skipped"], [{"thread_id": "active", "rollout_path": str(source),
                                             "history_db": None, "reason": "missing_cursor"}])
        self.assertEqual(projection.audit_projection(self.home)["issues"][0]["code"], "missing_history_db")
        with _database(self.state) as connection:
            connection.execute("UPDATE threads SET archived=1")
        with self.assertRaises(projection.ProjectionError):
            projection.invalidate_projection(self.home, ["active"], host_closed=True, force=True)

    def test_identity_and_expected_path_are_checked(self):
        source, _ = self._thread()
        wrong = self.home / "same-header-wrong-path.jsonl"
        wrong.write_bytes(source.read_bytes())
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True,
                                              rollout_paths={"active": wrong})
        self.assertEqual(error.exception.code, "source_path_mismatch")
        source.write_text('{"type":"session_meta","payload":{"id":"someone-else"}}\n')
        self.assertEqual(projection.audit_projection(self.home)["entries"][0]["status"], "source_identity_mismatch")
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "source_identity_mismatch")
        self.assertEqual(len(self._rows()), 1)

    def test_unknown_schema_missing_db_and_corruption_fail_closed(self):
        self._thread()
        with _database(self.history) as connection:
            connection.execute("ALTER TABLE thread_history_projection_state ADD COLUMN conversation TEXT")
        before = self._snapshot()
        self.assertEqual(projection.audit_projection(self.home)["issues"][0]["code"], "unknown_schema")
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "unknown_schema")
        self.assertEqual(before, self._snapshot())
        missing = self.home / "missing.sqlite"
        report = projection.audit_projection(self.home, history_db_paths=[missing])
        self.assertEqual(report["issues"][0]["code"], "missing_history_db")
        self.assertFalse(missing.exists())
        self.state.write_bytes(b"not a database")
        self.assertEqual(projection.audit_projection(self.home)["issues"][0]["code"], "unreadable_database")

    def test_state_without_archive_column_fails_closed(self):
        self._thread()
        with _database(self.state) as connection:
            connection.execute("ALTER TABLE threads RENAME COLUMN archived TO unknown_archive_flag")
        report = projection.audit_projection(self.home)
        self.assertEqual(report["issues"][0]["code"], "unknown_schema")
        with self.assertRaises(projection.ProjectionError):
            projection.invalidate_projection(self.home, ["active"], host_closed=True)

    def test_unknown_delete_trigger_is_refused_before_any_reset(self):
        self._thread()
        with _database(self.history) as connection:
            connection.execute("CREATE TRIGGER dangerous AFTER DELETE ON thread_history_projection_state "
                               "BEGIN DELETE FROM thread_history_items; END")
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "unknown_schema")
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(len(self._rows(table="thread_history_items")), 1)

    def test_update_trigger_is_refused_and_realtime_is_preserved(self):
        self._thread()
        with _database(self.history) as connection:
            connection.execute("CREATE TRIGGER dangerous_update AFTER UPDATE ON thread_history_projection_state "
                               "BEGIN DELETE FROM thread_realtime_items WHERE thread_id=OLD.thread_id; END")
        before = self._snapshot()
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "unknown_schema")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._rows(), [("active", 99999, 4)])
        self.assertEqual(len(self._rows(table="thread_realtime_items")), 1)

    def test_known_trigger_name_with_changed_body_is_refused(self):
        self._thread()
        with _database(self.history) as connection:
            connection.execute("DROP TRIGGER " + projection.KNOWN_DELETE_CLEANUP_TRIGGER)
            connection.execute("CREATE TRIGGER " + projection.KNOWN_DELETE_CLEANUP_TRIGGER +
                               " AFTER UPDATE ON thread_history_projection_state "
                               "BEGIN DELETE FROM thread_realtime_items WHERE thread_id=OLD.thread_id; END")
        with self.assertRaises(projection.ProjectionError) as error:
            projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "unknown_schema")
        self.assertEqual(self._rows(), [("active", 99999, 4)])
        self.assertEqual(len(self._rows(table="thread_realtime_items")), 1)

    def test_locked_database_does_not_change_another_database(self):
        self._thread()
        second = self.home / "thread_history_3.sqlite"
        self._history_db(second)
        with _database(second) as connection:
            connection.execute("INSERT INTO thread_history_projection_state VALUES ('active',99999,4)")
        lock = sqlite3.connect(second)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        with patch.object(projection, "SQLITE_TIMEOUT", 0.01):
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "database_error")
        self.assertEqual(error.exception.committed_history_dbs, [])
        self.assertEqual(len(self._rows()), 1)
        self.assertFalse((self.home / "projection-backups").exists())

    def test_second_update_failure_rolls_back_all_rows_in_database(self):
        self._thread("a")
        self._thread("b")
        original_connect = projection._connect
        class FailingConnection:
            def __init__(self, actual):
                self.actual = actual
                self.updates = 0
            def __getattr__(self, key):
                return getattr(self.actual, key)
            def execute(self, statement, *args):
                if statement.startswith("UPDATE"):
                    self.updates += 1
                    if self.updates == 2:
                        raise sqlite3.OperationalError("synthetic second-update failure")
                return self.actual.execute(statement, *args)
        def connect(path, *, readonly):
            actual = original_connect(path, readonly=readonly)
            return actual if readonly else FailingConnection(actual)
        with patch.object(projection, "_connect", side_effect=connect):
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["a", "b"], host_closed=True)
        self.assertEqual(self._rows(), [("a", 99999, 4), ("b", 99999, 4)])
        self.assertEqual(error.exception.committed_history_dbs, [])
        self.assertTrue(Path(error.exception.backup).is_file())

    def test_membership_is_rechecked_before_commit(self):
        self._thread()
        write_backup = projection._write_backup
        def archive(home, rows):
            result = write_backup(home, rows)
            with _database(self.state) as connection:
                connection.execute("UPDATE threads SET archived=1")
            return result
        with patch.object(projection, "_write_backup", side_effect=archive):
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "inactive_thread")
        self.assertEqual(self._rows(), [("active", 99999, 4)])

    def test_source_fingerprint_is_rechecked_before_commit(self):
        source, _ = self._thread()
        write_backup = projection._write_backup
        def append(home, rows):
            result = write_backup(home, rows)
            with source.open("ab") as stream:
                stream.write(b'{}\n')
            return result
        with patch.object(projection, "_write_backup", side_effect=append):
            with self.assertRaises(projection.ProjectionError) as error:
                projection.invalidate_projection(self.home, ["active"], host_closed=True)
        self.assertEqual(error.exception.code, "source_changed")
        self.assertEqual(self._rows(), [("active", 99999, 4)])

    def test_database_override_uri_handles_reserved_characters(self):
        self._thread()
        alternate = self.home / "state ?#.sqlite"
        self.state.rename(alternate)
        report = projection.audit_projection(self.home, state_db_path=alternate,
                                             history_db_paths=[self.history])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["active_threads"], 1)


if __name__ == "__main__":
    unittest.main()
