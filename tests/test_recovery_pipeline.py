"""Synthetic recovery pipeline tests; every host/process entry point is mocked."""
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from codex_switcher import history_rebuild, projection, recovery


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection


class RecoveryPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="recovery-pipeline-fixture-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "state_10.sqlite"
        self.history = self.home / "thread_history_2.sqlite"
        self.binary = self.home / "never-executed-codex"
        self.binary.write_text("synthetic executable placeholder")
        with database(self.state) as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,archived INTEGER,"
                       "model_provider TEXT,history_mode TEXT)")
        self.make_history(self.history)
        home_patch = patch.object(recovery.paths, "codex_home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        process_patch = patch.object(history_rebuild.subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden"))
        process_patch.start()
        self.addCleanup(process_patch.stop)

    def make_history(self, path):
        with database(path) as db:
            db.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,"
                       "next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)")
            db.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,payload TEXT,PRIMARY KEY(thread_id,turn_id))")
            db.execute("CREATE TABLE thread_items(thread_id TEXT,item_id TEXT,payload TEXT,PRIMARY KEY(thread_id,item_id))")
            db.execute("CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,payload TEXT,PRIMARY KEY(thread_id,item_id))")
            db.execute(projection.KNOWN_DELETE_CLEANUP_SQL)

    def thread(self, tid="active", *, archived=False, provider="openai", mode="paginated", offset=99999):
        source = self.home / (tid + ".jsonl")
        source.write_text(json.dumps({"type": "session_meta", "payload": {"id": tid, "model_provider": provider}}) +
                          '\n{"type":"response_item","payload":{"type":"message","content":[]}}\n')
        with database(self.state) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (tid, str(source), int(archived), provider, mode))
        with database(self.history) as db:
            db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", (tid, offset, 4))
            db.execute("INSERT INTO thread_turns VALUES (?,?,?)", (tid, "existing-turn", "original turn"))
            db.execute("INSERT INTO thread_items VALUES (?,?,?)", (tid, "existing-item", "original item"))
            db.execute("INSERT INTO thread_realtime_items VALUES (?,?,?)", (tid, "existing-live", "original live"))
        return source

    def rows(self, path=None):
        with database(path or self.history) as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY 1,2").fetchall()
                    for table in (*history_rebuild.TABLES, projection.PROJECTION_TABLE)}

    def rebuilt(self, tid="active", *, conflict=False):
        path = self.home / "rebuilt.sqlite"
        history_rebuild.backup_database(self.history, path)
        with database(path) as db:
            for table, key in (("thread_turns", "new-turn"), ("thread_items", "new-item"),
                               ("thread_realtime_items", "new-live")):
                db.execute("INSERT INTO " + table + " VALUES (?,?,?)", (tid, key, "reconstructed " + key))
            if conflict:
                db.execute("UPDATE thread_items SET payload='conflicting rebuilt body' WHERE thread_id=? AND item_id='existing-item'", (tid,))
        return path

    def fake_reader(self, counts=None):
        reader = MagicMock()
        reader._closed = False
        reader.close.side_effect = lambda: setattr(reader, '_closed', True)
        reader.counts.return_value = counts or {"turns": 2, "items": 4, "display_sha256": "matching-display"}
        return reader

    def failure_receipt(self):
        roots = list(self.home.glob('model-switcher-preservation/recovery-*'))
        self.assertEqual(len(roots), 1)
        path = roots[0] / 'failure.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        receipt = json.loads(path.read_text())
        report = json.loads((roots[0] / 'report.json').read_text())
        for frame in receipt['trace']:
            self.assertEqual(set(frame), {'file', 'function', 'line'})
            self.assertEqual(frame['file'], Path(frame['file']).name)
            self.assertIsInstance(frame['line'], int)
        self.assertTrue(receipt['trace'])
        return receipt, report

    def test_import_adds_missing_rows_preserves_existing_and_archived_and_realtime(self):
        self.thread()
        self.thread("archived", archived=True)
        rebuilt = self.rebuilt()
        before = self.rows()
        validate = MagicMock()
        result = history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True, validate_target=validate)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result, {"added_turns": 1, "added_items": 1, "added_realtime_items": 1})
        after = self.rows()
        for table in history_rebuild.TABLES:
            for row in before[table]:
                self.assertIn(row, after[table])
        self.assertEqual(after[projection.PROJECTION_TABLE], [("active", 0, 0), ("archived", 99999, 4)])
        repeat = history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True, validate_target=validate)
        self.assertEqual(repeat, {"added_turns": 0, "added_items": 0, "added_realtime_items": 0})
        self.assertEqual(self.rows(), after)

    def test_existing_item_content_is_not_overwritten_and_conflict_is_reported(self):
        self.thread()
        rebuilt = self.rebuilt(conflict=True)
        result = history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True,
                                                    validate_target=lambda: None)
        self.assertEqual(result["conflicting_items"], 1)
        self.assertIn(("active", "existing-item", "original item"), self.rows()["thread_items"])
        self.assertNotIn(("active", "existing-item", "conflicting rebuilt body"), self.rows()["thread_items"])

    def test_callback_failure_before_commit_rolls_back_inserts_and_cursor(self):
        self.thread()
        rebuilt = self.rebuilt()
        before = self.rows()
        validate = MagicMock(side_effect=[None, OSError("synthetic host reopened")])
        with self.assertRaises(OSError):
            history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True, validate_target=validate)
        self.assertEqual(self.rows(), before)

    def test_unknown_insert_trigger_rolls_back_earlier_table_inserts(self):
        self.thread()
        rebuilt = self.rebuilt()
        with database(self.history) as db:
            db.execute("CREATE TRIGGER unsafe AFTER INSERT ON thread_items BEGIN DELETE FROM thread_realtime_items; END")
        before = self.rows()
        with self.assertRaises(OSError):
            history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True, validate_target=lambda: None)
        self.assertEqual(self.rows(), before)

    def test_missing_live_database_is_not_created(self):
        self.thread()
        rebuilt = self.rebuilt()
        absent = self.home / "absent-live.sqlite"
        with self.assertRaises((OSError, sqlite3.Error)):
            history_rebuild.import_missing_rows(absent, rebuilt, "active", host_closed=True, validate_target=lambda: None)
        self.assertFalse(absent.exists())

    def test_import_without_verified_host_shutdown_never_connects(self):
        with patch.object(history_rebuild.sqlite3, "connect", side_effect=AssertionError("unexpected DB access")):
            with self.assertRaises(OSError):
                history_rebuild.import_missing_rows(self.history, self.history, "active", host_closed=False)

    def test_locked_live_database_refuses_import_without_partial_changes(self):
        self.thread()
        rebuilt = self.rebuilt()
        original = self.rows()
        lock = sqlite3.connect(self.history)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        connect = sqlite3.connect
        def quick_connect(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return connect(*args, **kwargs)
        with patch.object(history_rebuild.sqlite3, "connect", side_effect=quick_connect):
            with self.assertRaises(sqlite3.OperationalError):
                history_rebuild.import_missing_rows(self.history, rebuilt, "active", host_closed=True,
                                                    validate_target=lambda: None)
        self.assertEqual(self.rows(), original)

    def test_materialize_resumes_only_copy_with_rewritten_state_path(self):
        source = self.thread()
        with database(self.history) as db:
            db.execute('ALTER TABLE thread_turns ADD COLUMN rollout_byte_offset INTEGER NOT NULL DEFAULT 0')
            db.execute('ALTER TABLE thread_turns ADD COLUMN rollout_end_byte_offset INTEGER')
        (self.home / "auth.json").write_text("synthetic credentials must not enter replay home")
        destination = self.home / "private-copy"
        source_bytes = source.read_bytes()
        original_rows = self.rows()
        actual_counts = {"turns": 1, "items": 2, "display_sha256": "fixture-display"}
        seen = []
        outer = self
        class CopyReader:
            def __init__(self, binary, home):
                self.home = Path(home)
                outer.assertEqual(self.home, destination)
                outer.assertFalse((self.home / "auth.json").exists())
            def rpc(self, method, params, timeout=60):
                seen.append((method, params.copy()))
                outer.assertEqual(method, "thread/resume")
                with database(self.home / outer.state.name) as db:
                    path = Path(db.execute("SELECT rollout_path FROM threads WHERE id='active'").fetchone()[0])
                outer.assertEqual(path, destination / "sessions" / source.name)
                outer.assertNotEqual(path, source)
                with database(self.home / outer.history.name) as db:
                    for table in (*history_rebuild.TABLES, projection.PROJECTION_TABLE):
                        outer.assertEqual(db.execute("SELECT * FROM " + table).fetchall(), [])
                    for table in history_rebuild.TABLES:
                        for row in original_rows[table]:
                            db.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", row)
                    db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", ("active", 0, 0))
                with path.open("ab") as stream:
                    stream.write(b'{"type":"event_msg","payload":{"type":"thread_settings"}}\n')
                return {}
            def counts(self, tid):
                outer.assertEqual(tid, "active")
                return actual_counts
            def close(self):
                pass
        with patch.object(history_rebuild, "LocalReader", CopyReader):
            result = history_rebuild.materialize_copy(self.binary, self.state, self.history, source, "active", destination)
        self.assertEqual(result["source_sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(result["display_before"], actual_counts)
        self.assertEqual(result["display"], actual_counts)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(self.rows(), original_rows)
        self.assertEqual([method for method, _ in seen], ["thread/resume"])
        self.assertEqual(self.rows(destination / "history-before-replay.sqlite"), original_rows)
        for table in history_rebuild.TABLES:
            self.assertEqual(self.rows(destination / self.history.name)[table], original_rows[table])
        self.assertEqual(self.rows(destination / self.history.name)[projection.PROJECTION_TABLE], [("active", 0, 0)])

    def test_materialize_refuses_archived_snapshot_before_starting_reader(self):
        source = self.thread(archived=True)
        with patch.object(history_rebuild, "LocalReader", side_effect=AssertionError("archived replay attempted")):
            with self.assertRaises(OSError):
                history_rebuild.materialize_copy(self.binary, self.state, self.history, source, "active", self.home / "copy")

    def test_materialize_rejects_identity_mismatch_without_creating_destination(self):
        source = self.thread()
        destination = self.home / "copy"
        with patch.object(history_rebuild, "LocalReader", side_effect=AssertionError("mismatched replay attempted")):
            with self.assertRaises(OSError):
                history_rebuild.materialize_copy(self.binary, self.state, self.history, source, "wrong-id", destination)
        self.assertFalse(destination.exists())

    def test_run_scope_is_active_paginated_and_preserves_archived_source(self):
        source = self.thread(provider="third-party")
        archived = self.thread("archived", archived=True)
        legacy = self.thread("legacy", mode="legacy")
        archived_bytes, legacy_bytes = archived.read_bytes(), legacy.read_bytes()
        rebuilt = self.rebuilt()
        replay = {"thread_id": "active", "source_sha256": history_rebuild.digest(source),
                  "history_copy": str(rebuilt), "display": {"turns": 2, "items": 4, "display_sha256": "matching-display"},
                  "display_before": {"turns": 1, "items": 2, "display_sha256": "before-display"}}
        reader = self.fake_reader()
        with patch.object(recovery, "_require_closed"), \
                patch.object(history_rebuild, "materialize_copy", return_value=replay) as materialize, \
                patch.object(history_rebuild, "LocalReader", return_value=reader), \
                patch.object(recovery.message_ids, "repair_file", side_effect=AssertionError("third party or archived source rewrite")):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        self.assertEqual(result["phase"], "done")
        self.assertEqual({r["thread_id"] for r in result["tasks"]}, {"active", "legacy"})
        self.assertEqual(next(r for r in result["tasks"] if r["thread_id"] == "legacy")["status"], "legacy-unchanged")
        self.assertEqual(materialize.call_args.args[3:5], (source, "active"))
        active_row = next(r for r in result['tasks'] if r['thread_id'] == 'active')
        self.assertEqual(active_row['before_display'], reader.counts.return_value)
        self.assertEqual(active_row['copy_before_display'], replay['display_before'])
        self.assertEqual(active_row['source_after_sha256'], history_rebuild.digest(source))
        self.assertEqual([call.args for call in reader.counts.call_args_list], [('active',), ('active',)])
        self.assertEqual(archived.read_bytes(), archived_bytes)
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        self.assertIn(("archived", 99999, 4), self.rows()[projection.PROJECTION_TABLE])

    def test_run_source_change_before_import_is_rejected_without_live_import(self):
        source = self.thread(provider="third-party")
        rebuilt = self.rebuilt()
        original = self.rows()
        def replay(*args):
            expected = history_rebuild.digest(source)
            with source.open("ab") as stream:
                stream.write(b'{}\n')
            return {"source_sha256": expected, "history_copy": str(rebuilt),
                    "display": {"turns": 2, "items": 4, "display_sha256": "matching-display"}}
        with patch.object(recovery, "_require_closed"), \
                patch.object(history_rebuild, "materialize_copy", side_effect=replay), \
                patch.object(history_rebuild, "LocalReader", return_value=self.fake_reader()):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["tasks"][0]["status"], "needs-review")
        self.assertEqual(self.rows(), original)

    def test_run_archive_change_before_import_is_rejected(self):
        source = self.thread(provider="third-party")
        rebuilt = self.rebuilt()
        original = self.rows()
        def replay(*args):
            with database(self.state) as db:
                db.execute("UPDATE threads SET archived=1 WHERE id='active'")
            return {"source_sha256": history_rebuild.digest(source), "history_copy": str(rebuilt),
                    "display": {"turns": 2, "items": 4, "display_sha256": "matching-display"}}
        with patch.object(recovery, "_require_closed"), \
                patch.object(history_rebuild, "materialize_copy", side_effect=replay), \
                patch.object(history_rebuild, "LocalReader", return_value=self.fake_reader()):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(self.rows(), original)

    def test_host_reopening_during_replay_blocks_live_import(self):
        source = self.thread(provider="third-party")
        rebuilt = self.rebuilt()
        original = self.rows()
        host = {"running": False}
        def require_closed():
            if host["running"]:
                raise OSError("synthetic host reopened")
        def replay(*args):
            host["running"] = True
            return {"source_sha256": history_rebuild.digest(source), "history_copy": str(rebuilt),
                    "display": {"turns": 2, "items": 4, "display_sha256": "matching-display"}}
        before_reader = self.fake_reader()
        def reader_while_closed(*args):
            self.assertFalse(host['running'])
            return before_reader
        with patch.object(recovery, "_require_closed", side_effect=require_closed), \
                patch.object(history_rebuild, "materialize_copy", side_effect=replay), \
                patch.object(history_rebuild, "LocalReader", side_effect=reader_while_closed):
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(self.rows(), original)

    def test_database_exception_becomes_task_failure_not_incomplete_global_exit(self):
        source = self.thread(provider="third-party")
        replay = {"source_sha256": history_rebuild.digest(source), "history_copy": "synthetic-unused",
                  "display": {"turns": 2, "items": 4, "display_sha256": "matching-display"}}
        with patch.object(recovery, "_require_closed"), \
                patch.object(history_rebuild, "materialize_copy", return_value=replay), \
                patch.object(history_rebuild, "import_missing_rows", side_effect=sqlite3.OperationalError("synthetic lock")), \
                patch.object(history_rebuild, "LocalReader", return_value=self.fake_reader()):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["tasks"][0]["status"], "needs-review")
        self.assertEqual(result["tasks"][0]["error_type"], "OperationalError")

    def test_equal_counts_with_different_display_hash_cannot_be_verified(self):
        source = self.thread(provider="third-party")
        original_rows = self.rows()
        rebuilt = self.rebuilt()
        replay = {"source_sha256": history_rebuild.digest(source), "history_copy": str(rebuilt),
                  "display": {"turns": 2, "items": 4, "display_sha256": "expected-display"}}
        reader = self.fake_reader({"turns": 2, "items": 4, "display_sha256": "different-display"})
        with patch.object(recovery, "_require_closed"), \
                patch.object(history_rebuild, "materialize_copy", return_value=replay), \
                patch.object(history_rebuild, "LocalReader", return_value=reader):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result["phase"], "partial")
        self.assertEqual(result["tasks"][0]["status"], "needs-review")
        self.assertEqual(result['tasks'][0]['rollback']['status'], 'completed')
        self.assertTrue(result['tasks'][0]['rollback']['result']['rolled_back'])
        self.assertEqual(self.rows(), original_rows)

    def test_all_eligible_raw_sources_are_backed_up_before_first_repair(self):
        first = self.thread()
        second = self.thread('no-protocol-change', offset=0)
        archived = self.thread('archived', archived=True)
        legacy = self.thread('legacy', mode='legacy')
        originals = {path: path.read_bytes() for path in (first, second, archived, legacy)}
        rebuilt = self.rebuilt()
        replay = {'source_sha256': history_rebuild.digest(first), 'history_copy': str(rebuilt),
                  'display': {'turns': 2, 'items': 4, 'display_sha256': 'matching-display'}}
        def repair(path, backup_dir, **kwargs):
            backup_root = Path(backup_dir).parent
            manifest = json.loads((backup_root / 'report.json').read_text())
            snapshots = manifest['raw_rollout_snapshots']
            self.assertEqual(set(snapshots), {'active', 'no-protocol-change'})
            for tid, source in (('active', first), ('no-protocol-change', second)):
                entry = snapshots[tid]
                raw_backup = Path(entry['backup'])
                self.assertEqual(raw_backup.read_bytes(), originals[source])
                self.assertEqual(entry['sha256'], hashlib.sha256(originals[source]).hexdigest())
                self.assertNotEqual(raw_backup.stat().st_ino, source.stat().st_ino)
                self.assertEqual(stat.S_IMODE(raw_backup.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(raw_backup.parent.stat().st_mode), 0o700)
                sidecar = json.loads(raw_backup.with_suffix('.json').read_text())
                self.assertEqual(sidecar['sha256'], entry['sha256'])
            return {'changed': 0}
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=repair) as normalize, \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(result['raw_rollout_snapshot_count'], 2)
        for row in result['tasks']:
            if row['thread_id'] in result['raw_rollout_snapshots']:
                self.assertEqual(row['source_after_sha256'], row['source_before_sha256'])
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_raw_backup_failure_aborts_all_real_modification(self):
        self.thread()
        self.thread('second')
        original_rows = self.rows()
        snapshot = recovery._snapshot_rollout
        seen = []
        def fail_second(*args):
            seen.append(args)
            if len(seen) == 2:
                raise OSError('synthetic backup failure')
            return snapshot(*args)
        progress = MagicMock()
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery, '_snapshot_rollout', side_effect=fail_second), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'materialize_copy') as materialize, \
                patch.object(history_rebuild, 'import_missing_rows') as imported, \
                patch.object(history_rebuild, 'LocalReader') as reader:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress, normalize_official=True)
        for operation in (normalize, materialize, imported, reader):
            operation.assert_not_called()
        self.assertEqual(self.rows(), original_rows)
        failure, report = self.failure_receipt()
        self.assertEqual(failure['stage'], 'raw-snapshot')
        self.assertEqual(failure['reason_code'], 'unclassified_os_error')
        self.assertEqual(failure['backup_count'], 3)
        self.assertEqual(report['raw_rollout_snapshot_count'], 1)
        self.assertEqual(report['database_snapshot_count'], 2)
        self.assertEqual(report['backup_count'], 3)
        self.assertEqual(report['phase'], 'snapshotting')
        self.assertEqual(progress.call_args.kwargs['phase'], 'error')
        self.assertEqual(progress.call_args.kwargs['stage'], 'raw-snapshot')
        self.assertNotIn('synthetic backup failure', json.dumps(failure))

    def test_backup_readback_corruption_blocks_every_repair(self):
        self.thread()
        digest = history_rebuild.digest
        def wrong_backup_hash(path):
            if Path(path).parent.name == 'raw-rollouts' and Path(path).suffix == '.jsonl':
                return 'corrupt-backup'
            return digest(path)
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'digest', side_effect=wrong_backup_hash), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'materialize_copy') as materialize:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary)
        normalize.assert_not_called()
        materialize.assert_not_called()

    def test_source_changed_after_raw_backup_aborts_before_first_repair(self):
        source = self.thread()
        original_rows = self.rows()
        snapshot = recovery._snapshot_rollout
        def append_after_backup(*args):
            result = snapshot(*args)
            with source.open('ab') as stream:
                stream.write(b'{}\n')
            return result
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery, '_snapshot_rollout', side_effect=append_after_backup), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'materialize_copy') as materialize:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary)
        normalize.assert_not_called()
        materialize.assert_not_called()
        self.assertEqual(self.rows(), original_rows)

    def test_plaintext_only_reasoning_refusal_keeps_source_but_recovers_pagination(self):
        source = self.thread(offset=0)
        with source.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'reasoning',
                         'content': [{'type': 'reasoning_text', 'text': 'synthetic retained body'}]}}) + '\n')
        original = source.read_bytes()
        rebuilt = self.rebuilt()
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display_before': {'turns': 1, 'items': 2, 'display_sha256': 'before-display'},
                  'display': {'turns': 2, 'items': 4, 'display_sha256': 'matching-display'}}
        # Use the actual normalizer; its explicit refusal occurs before any write.
        from codex_switcher import history
        with patch.object(recovery, '_require_closed'), patch.object(history, 'busy_reason', return_value=None), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay) as materialize, \
                patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()), \
                patch.object(history_rebuild, 'rollback_import') as rollback:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        materialize.assert_called_once()
        rollback.assert_not_called()
        self.assertEqual(result['rebuilt'], 1)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(result['failed'], 1)
        row = result['tasks'][0]
        self.assertEqual(row['status'], 'needs-review')
        self.assertEqual(row['display_status'], 'verified')
        self.assertEqual(row['protocol_needs_review'], 'plaintext_reasoning_requires_retained_encrypted_content')
        self.assertEqual(row['before_display'], self.fake_reader().counts.return_value)
        self.assertEqual(row['copy_before_display'], replay['display_before'])
        self.assertEqual(row['source_after_sha256'], row['source_before_sha256'])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(Path(row['raw_backup']).read_bytes(), original)
        self.assertIn(('active', 'new-item', 'reconstructed new-item'), self.rows()['thread_items'])

    def test_provider_metadata_disagreement_preserves_raw_and_recovers_pagination(self):
        source = self.thread(provider='third-party')
        with database(self.state) as db:
            db.execute("UPDATE threads SET model_provider='openai' WHERE id='active'")
        original = source.read_bytes()
        before_rows = self.rows()
        rebuilt = self.rebuilt()
        before_display = {'turns': 1, 'items': 2, 'display_sha256': 'before-display'}
        after_display = {'turns': 2, 'items': 4, 'display_sha256': 'matching-display'}
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display_before': before_display, 'display': after_display}
        reader = self.fake_reader()
        reads = []

        def fake_read(tid):
            self.assertEqual(tid, 'active')
            self.assertEqual(source.read_bytes(), original)
            reads.append(tid)
            return before_display if len(reads) == 1 else after_display

        reader.counts.side_effect = fake_read
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=AssertionError('provider mismatch must not normalize')) as normalize, \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay) as materialize, \
                patch.object(history_rebuild, 'LocalReader', return_value=reader), \
                patch.object(history_rebuild, 'rollback_import') as rollback:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        normalize.assert_not_called()
        materialize.assert_called_once()
        rollback.assert_not_called()
        self.assertEqual(reads, ['active', 'active'])
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(result['rebuilt'], 1)
        row = result['tasks'][0]
        self.assertEqual(row['status'], 'needs-review')
        self.assertEqual(row['protocol_needs_review'], 'provider_metadata_disagreement')
        self.assertEqual(row['display_status'], 'verified')
        self.assertEqual(row['before_display'], before_display)
        self.assertEqual(row['verified_display'], after_display)
        self.assertEqual(row['source_after_sha256'], row['source_before_sha256'])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(Path(row['raw_backup']).read_bytes(), original)
        after_rows = self.rows()
        for table in history_rebuild.TABLES:
            for existing in before_rows[table]:
                self.assertIn(existing, after_rows[table])
        self.assertIn(('active', 'new-item', 'reconstructed new-item'), after_rows['thread_items'])
        with database(self.state) as db:
            self.assertEqual(db.execute("SELECT model_provider FROM threads WHERE id='active'").fetchone(), ('openai',))

    def test_other_normalization_failure_does_not_enter_pagination_recovery(self):
        self.thread()
        for error in (ValueError('different rejection'), OSError('synthetic IO failure')):
            with self.subTest(error=type(error).__name__), patch.object(recovery, '_require_closed'), \
                    patch.object(recovery.message_ids, 'repair_file', side_effect=error), \
                    patch.object(history_rebuild, 'materialize_copy') as materialize, \
                    patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()):
                result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
            materialize.assert_not_called()
            self.assertEqual(result['tasks'][0]['status'], 'needs-review')
            self.assertNotIn('protocol_needs_review', result['tasks'][0])

    def test_plaintext_refusal_after_source_mutation_cannot_fall_back(self):
        source = self.thread()
        original_rows = self.rows()
        def changed_then_refused(*args, **kwargs):
            with source.open('ab') as stream:
                stream.write(b'{}\n')
            raise ValueError(recovery._PLAINTEXT_REASONING_REJECTION)
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=changed_then_refused), \
                patch.object(history_rebuild, 'materialize_copy') as materialize, \
                patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        materialize.assert_not_called()
        self.assertEqual(result['tasks'][0]['status'], 'needs-review')
        self.assertNotIn('protocol_needs_review', result['tasks'][0])
        self.assertEqual(self.rows(), original_rows)

    def test_all_real_display_baselines_are_read_after_backups_before_any_repair(self):
        first = self.thread(offset=0)
        second = self.thread('second', offset=0)
        self.thread('archived', archived=True)
        self.thread('legacy', mode='legacy')
        before_reader = self.fake_reader({'turns': 1, 'items': 2, 'display_sha256': 'baseline'})
        after_reader = self.fake_reader({'turns': 1, 'items': 2, 'display_sha256': 'baseline'})
        order = []
        def counts(tid):
            self.assertEqual(len(list(self.home.glob('model-switcher-preservation/*/raw-rollouts/*.jsonl'))), 2)
            self.assertNotIn('repair', order)
            order.append(tid)
            return {'turns': 1, 'items': 2, 'display_sha256': 'baseline'}
        before_reader.counts.side_effect = counts
        def normalize(*args, **kwargs):
            self.assertEqual(order[:2], ['active', 'second'])
            before_reader.close.assert_called_once()
            order.append('repair')
            return {'changed': 0}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[before_reader, after_reader]) as readers, \
                patch.object(recovery.message_ids, 'repair_file', side_effect=normalize), \
                patch.object(history_rebuild, 'materialize_copy') as replay:
                result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        replay.assert_not_called()
        self.assertEqual(readers.call_count, 2)
        before_reader.rpc.assert_not_called()
        self.assertEqual(set(result['before_display']), {'active', 'second'})
        for row in result['tasks']:
            if row['thread_id'] in {'active', 'second'}:
                self.assertEqual(row['before_display'], {'turns': 1, 'items': 2, 'display_sha256': 'baseline'})
                self.assertEqual(row['source_counts_after'], row['source_counts_before'])
                self.assertEqual(row['source_after_sha256'], row['source_before_sha256'])

    def test_before_display_error_is_reported_but_does_not_skip_safe_recovery(self):
        source = self.thread(provider='third-party')
        rebuilt = self.rebuilt()
        before_reader = self.fake_reader()
        before_reader.counts.side_effect = OSError('synthetic private upstream detail')
        after_reader = self.fake_reader()
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display_before': {'turns': 9, 'items': 9, 'display_sha256': 'must-not-overwrite-real-before'},
                  'display': after_reader.counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[before_reader, after_reader]), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result['phase'], 'done')
        self.assertEqual(result['tasks'][0]['before_display'], {'error': 'OSError'})
        self.assertEqual(result['tasks'][0]['copy_before_display'], replay['display_before'])
        self.assertNotIn('synthetic private upstream detail', json.dumps(result))

    def test_raw_metrics_fully_parse_types_and_compacted_items_without_bodies(self):
        source = self.thread()
        with source.open('a') as stream:
            stream.write('\n')
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call',
                                      'arguments': 'synthetic private text'}}) + '\n')
            stream.write(json.dumps({'type': 'compacted', 'payload': {
                'replacement_history': [{'type': 'message', 'content': 'synthetic private body'}, {'type': 'function_call_output'}],
                'guardian_history': None}}) + '\n')
        metrics = recovery._raw_metrics(source)
        counts = metrics['record_counts']
        self.assertEqual(counts['records'], 4)
        self.assertEqual(counts['blank_lines'], 1)
        self.assertEqual(counts['types'], {'session_meta': 1, 'response_item': 2, 'compacted': 1})
        self.assertEqual(counts['payload_types']['function_call'], 1)
        self.assertEqual(counts['type_payload_pairs']['response_item'], {'message': 1, 'function_call': 1})
        self.assertEqual(counts['compacted_history_types']['replacement_history'], {'message': 1, 'function_call_output': 1})
        self.assertEqual(metrics['sha256'], history_rebuild.digest(source))
        self.assertNotIn('synthetic private', json.dumps(metrics))

    def test_malformed_late_jsonl_record_blocks_before_any_reader_or_repair(self):
        source = self.thread()
        with source.open('ab') as stream:
            stream.write(b'{malformed late record}\n')
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'LocalReader') as reader:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary)
        normalize.assert_not_called()
        reader.assert_not_called()

    def test_normalizer_record_loss_blocks_import_and_retains_original_raw_backup(self):
        source = self.thread()
        original = source.read_bytes()
        def bad_normalizer(*args, **kwargs):
            source.write_bytes(original.splitlines(keepends=True)[0])
            return {'changed': 1}
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=bad_normalizer), \
                patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()), \
                patch.object(history_rebuild, 'materialize_copy') as replay:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        replay.assert_not_called()
        row = result['tasks'][0]
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(row['status'], 'needs-review')
        self.assertEqual(row['source_counts_before']['records'], 2)
        self.assertEqual(row['source_counts_after']['records'], 1)
        self.assertEqual(Path(row['raw_backup']).read_bytes(), original)

    def test_final_reader_close_source_change_cannot_report_verified(self):
        source = self.thread(provider='third-party', offset=0)
        original = source.read_bytes()
        before_reader = self.fake_reader()
        after_reader = self.fake_reader()
        # Keep every structural count unchanged; the post-close hash must catch it.
        after_reader.close.side_effect = lambda: source.write_bytes(original.replace(b'third-party', b'other-party'))
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[before_reader, after_reader]):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        row = result['tasks'][0]
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(row['status'], 'needs-review')
        self.assertEqual(row['display_status'], 'needs-review')
        self.assertEqual(row['source_counts_after'], row['source_counts_before'])
        self.assertNotEqual(row['source_after_sha256'], row['source_before_sha256'])

    def test_replay_destination_uses_hashed_identity(self):
        source = self.thread(provider='third-party')
        tid = '../unsafe-thread-id'
        data = source.read_bytes().replace(b'"id": "active"', b'"id": "../unsafe-thread-id"')
        source.write_bytes(data)
        with database(self.state) as db:
            db.execute('UPDATE threads SET id=? WHERE id=?', (tid, 'active'))
        with database(self.history) as db:
            for table in (*history_rebuild.TABLES, projection.PROJECTION_TABLE):
                db.execute('UPDATE ' + table + ' SET thread_id=? WHERE thread_id=?', (tid, 'active'))
        rebuilt = self.rebuilt(tid)
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': self.fake_reader().counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', return_value=self.fake_reader()), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay) as materialize:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        destination = Path(materialize.call_args.args[5])
        self.assertEqual(destination.name, hashlib.sha256(tid.encode()).hexdigest())
        self.assertEqual(destination.parent, Path(result['backup_root']) / 'replay')

    def test_closed_guard_also_checks_history_workers_and_handles(self):
        with patch.object(recovery.sys, 'platform', 'darwin'), \
                patch.object(recovery.codexapp, 'is_running', return_value=False), \
                patch.object(recovery.codexapp, 'assert_history_idle', side_effect=OSError('synthetic worker')) as idle:
            with self.assertRaises(OSError):
                recovery._require_closed()
        idle.assert_called_once_with(self.home)

    def test_undo_receipt_from_failed_import_is_rechecked_after_reader_close(self):
        source = self.thread(provider='third-party')
        original_rows = self.rows()
        rebuilt = self.rebuilt()
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': self.fake_reader().counts.return_value}
        write_undo = history_rebuild._write_import_undo
        rollback = history_rebuild.rollback_import
        readers = [self.fake_reader(), self.fake_reader()]
        def written_then_failed(*args):
            write_undo(*args)
            raise OSError('synthetic failure before database commit')
        def restore_after_close(*args, **kwargs):
            readers[-1].close.assert_called_once()
            return rollback(*args, **kwargs)
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=readers), \
                patch.object(history_rebuild, '_write_import_undo', side_effect=written_then_failed), \
                patch.object(history_rebuild, 'rollback_import', side_effect=restore_after_close) as restore:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        row = result['tasks'][0]
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(result['failed'], 1)
        restore.assert_called_once()
        self.assertTrue(Path(row['undo_receipt']).is_file())
        self.assertEqual(row['rollback']['status'], 'completed')
        self.assertTrue(row['rollback']['result']['already_restored'])
        self.assertEqual(self.rows(), original_rows)

    def test_readback_failure_never_rolls_back_concurrent_item_changes(self):
        source = self.thread(provider='third-party')
        rebuilt = self.rebuilt()
        before_reader, after_reader = self.fake_reader(), self.fake_reader()
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': {'turns': 2, 'items': 4, 'display_sha256': 'expected-display'}}
        def concurrent_readback(tid):
            with database(self.history) as db:
                db.execute("UPDATE thread_items SET payload='concurrent preserved value' "
                           "WHERE thread_id=? AND item_id='new-item'", (tid,))
            return {'turns': 2, 'items': 4, 'display_sha256': 'different-display'}
        after_reader.counts.side_effect = concurrent_readback
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[before_reader, after_reader]):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(result['tasks'][0]['rollback'], {'status': 'deferred', 'error_type': 'OSError',
                                                        'reason_code': 'import_afterimage_changed'})
        self.assertFalse(result['reopen_allowed'])
        self.assertEqual(result['pending_undo_count'], 1)
        self.assertIn(('active', 'new-item', 'concurrent preserved value'), self.rows()['thread_items'])
        self.assertIn(('active', 'existing-item', 'original item'), self.rows()['thread_items'])

    def test_reader_counting_never_resumes_or_starts_inference(self):
        reader = object.__new__(history_rebuild.LocalReader)
        reader.rpc = MagicMock(side_effect=[{"data": [{"id": "turn", "items": [{"id": "item"}]}], "nextCursor": "next"},
                                           {"data": [], "nextCursor": None}])
        result = reader.counts("active")
        self.assertEqual((result["turns"], result["items"]), (1, 1))
        self.assertEqual([call.args[0] for call in reader.rpc.call_args_list], ["thread/turns/list", "thread/turns/list"])
        self.assertEqual(reader.rpc.call_args_list[1].args[1]["cursor"], "next")
        forbidden = object.__new__(history_rebuild.LocalReader)
        forbidden.process = MagicMock()
        with self.assertRaises(ValueError):
            forbidden.rpc("turn/start", {"threadId": "active"})
        forbidden.process.stdin.write.assert_not_called()


    def test_database_snapshot_failure_keeps_completed_count_and_precise_receipt(self):
        source = self.thread()
        before, raw = self.rows(), source.read_bytes()
        real_backup = history_rebuild.backup_database
        original_error = OSError('Recovery snapshot verification failed')
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise original_error
            return real_backup(*args, **kwargs)

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'backup_database', side_effect=fail_second), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'import_missing_rows') as imported, \
                patch.object(history_rebuild, 'LocalReader') as reader:
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary)
        self.assertIs(raised.exception, original_error)
        for operation in (normalize, imported, reader):
            operation.assert_not_called()
        failure, report = self.failure_receipt()
        self.assertEqual((failure['stage'], failure['reason_code'], failure['backup_count']),
                         ('database-snapshot', 'database_snapshot_verification_failed', 1))
        self.assertEqual(report['phase'], 'snapshotting')
        self.assertEqual(report['database_snapshot_count'], 1)
        self.assertEqual(report['backup_count'], 1)
        self.assertEqual(report['checked'], 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(source.read_bytes(), raw)

    def test_source_verification_failure_publishes_all_backups_before_refusing_writes(self):
        source = self.thread()
        self.thread('second')
        before, raw = self.rows(), source.read_bytes()
        progress = MagicMock()
        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery, '_verify_raw_snapshot',
                             side_effect=OSError('Raw backup or original source changed before recovery')), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'import_missing_rows') as imported, \
                patch.object(history_rebuild, 'LocalReader') as reader:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress)
        for operation in (normalize, imported, reader):
            operation.assert_not_called()
        failure, report = self.failure_receipt()
        self.assertEqual(failure['stage'], 'source-verification')
        self.assertEqual(failure['reason_code'], 'source_or_backup_changed_before_recovery')
        self.assertEqual(report['raw_rollout_snapshot_count'], 2)
        self.assertEqual(report['backup_count'], 4)
        self.assertEqual(failure['backup_count'], 4)
        stages = [call.kwargs['stage'] for call in progress.call_args_list]
        self.assertIn('database-snapshot', stages)
        self.assertIn('raw-snapshot', stages)
        self.assertNotIn('baseline-open', stages)
        self.assertEqual(self.rows(), before)
        self.assertEqual(source.read_bytes(), raw)

    def test_baseline_close_failure_is_durable_and_never_enters_repair(self):
        source = self.thread()
        raw, before = source.read_bytes(), self.rows()
        reader = self.fake_reader()
        reader.close.side_effect = OSError('History reader process group did not exit')
        progress = MagicMock()
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', return_value=reader), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'import_missing_rows') as imported:
            with self.assertRaises(OSError):
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress)
        normalize.assert_not_called()
        imported.assert_not_called()
        self.assertEqual(reader.close.call_count, 2)
        failure, report = self.failure_receipt()
        self.assertEqual((failure['stage'], failure['reason_code']),
                         ('baseline-close', 'reader_group_still_running'))
        self.assertEqual(report['stage'], 'baseline-close')
        self.assertEqual(report['raw_rollout_snapshot_count'], 1)
        self.assertEqual(report['backup_count'], 3)
        self.assertEqual(set(report['raw_rollout_snapshots']), {'active'})
        self.assertEqual(report['phase'], 'snapshotting')
        stages = [call.kwargs['stage'] for call in progress.call_args_list]
        for stage in ('source-verification', 'baseline-open', 'baseline-read', 'baseline-close'):
            self.assertIn(stage, stages)
        self.assertEqual(self.rows(), before)
        self.assertEqual(source.read_bytes(), raw)

    def test_baseline_guard_primary_survives_cleanup_error_with_separate_safe_details(self):
        source = self.thread()
        raw, before = source.read_bytes(), self.rows()
        reader = self.fake_reader()
        reader.close.side_effect = OSError('History reader process group did not exit')
        primary = OSError('Desktop host worker is still running')
        observed_stage = None

        def progress(**values):
            nonlocal observed_stage
            observed_stage = values['stage']

        def guard():
            if observed_stage == 'baseline-read':
                raise primary

        with patch.object(recovery, '_require_closed', side_effect=guard), \
                patch.object(history_rebuild, 'LocalReader', return_value=reader), \
                patch.object(recovery.message_ids, 'repair_file') as normalize, \
                patch.object(history_rebuild, 'import_missing_rows') as imported:
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress)
        self.assertIs(raised.exception, primary)
        normalize.assert_not_called()
        imported.assert_not_called()
        reader.counts.assert_not_called()
        self.assertEqual(reader.close.call_count, 2)
        failure, report = self.failure_receipt()
        self.assertEqual((failure['stage'], failure['reason_code']), ('baseline-read', 'host_worker_running'))
        self.assertEqual(failure['cleanup_error'], {'error_type': 'OSError',
                                                   'reason_code': 'reader_group_still_running'})
        self.assertEqual(report['stage'], 'baseline-close')
        self.assertEqual(report['checked'], 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(source.read_bytes(), raw)

    def test_unknown_exception_and_chained_bodies_are_never_written_to_failure_or_progress(self):
        self.thread()
        sensitive = 'synthetic-secret-history-and-token'
        progress = MagicMock()

        def unexpected(*args, **kwargs):
            try:
                raise ValueError(sensitive)
            except ValueError as cause:
                raise RuntimeError(sensitive) from cause

        with patch.object(recovery, '_require_closed'), \
                patch.object(recovery, '_collect_before_display', side_effect=unexpected), \
                patch.object(recovery.message_ids, 'repair_file') as normalize:
            with self.assertRaises(RuntimeError):
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress)
        normalize.assert_not_called()
        failure, report = self.failure_receipt()
        self.assertEqual((failure['stage'], failure['reason_code']), ('baseline-read', 'unclassified_exception'))
        self.assertNotIn(sensitive, json.dumps(failure))
        self.assertNotIn(sensitive, json.dumps(report))
        self.assertNotIn(sensitive, json.dumps(progress.call_args.kwargs))
        self.assertEqual(set(failure['trace'][-1]), {'file', 'function', 'line'})

    def test_broken_progress_observer_does_not_skip_reader_cleanup_or_mask_failure(self):
        self.thread()
        reader = self.fake_reader()
        original_error = RuntimeError('synthetic observer private body')
        opening_seen = False

        def progress(**values):
            nonlocal opening_seen
            if values['stage'] == 'baseline-open':
                opening_seen = True
            if opening_seen and values['stage'] == 'baseline-read':
                raise original_error

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'LocalReader', return_value=reader), \
                patch.object(recovery.message_ids, 'repair_file') as normalize:
            with self.assertRaises(RuntimeError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress)
        self.assertIs(raised.exception, original_error)
        reader.close.assert_called_once()
        normalize.assert_not_called()
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['stage'], 'baseline-read')
        self.assertNotIn('synthetic observer private body', json.dumps(failure))


    def test_committed_import_is_undone_when_final_reader_init_fails_without_a_child(self):
        source = self.thread(provider='third-party')
        before = self.rows()
        rebuilt = self.rebuilt()
        baseline = self.fake_reader()
        primary = OSError('Local history reader exited')
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': baseline.counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[baseline, primary]):
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertIs(raised.exception, primary)
        self.assertEqual(self.rows(), before)
        failure, report = self.failure_receipt()
        self.assertEqual(failure['stage'], 'display-readback')
        self.assertEqual(failure['cleanup']['pending_undo_count'], 0)
        self.assertEqual(failure['cleanup']['tasks'][0]['rollback']['status'], 'completed')
        self.assertTrue(Path(report['tasks'][0]['undo_receipt']).is_file())

    def test_failed_constructor_live_child_defers_undo_and_reopen_without_touching_inserted_rows(self):
        source = self.thread(provider='third-party')
        rebuilt = self.rebuilt()
        baseline, failed_child = self.fake_reader(), self.fake_reader()
        failed_child.close.side_effect = OSError('History reader process group did not exit')
        primary = OSError('Local history reader exited')
        calls = 0

        def construct(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return baseline
            history_rebuild.register_local_reader(failed_child)
            primary._recovery_owned_reader = failed_child
            raise primary

        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': baseline.counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=construct), \
                patch.object(history_rebuild, 'rollback_import') as rollback:
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        rollback.assert_not_called()
        self.assertIs(raised.exception, primary)
        self.assertFalse(primary._recovery_reopen_allowed)
        self.assertFalse(failed_child._closed)
        self.assertIn(('active', 'new-item', 'reconstructed new-item'), self.rows()['thread_items'])
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['cleanup']['phase'], 'rollback-deferred')
        self.assertEqual(failure['cleanup']['pending_undo_count'], 1)
        self.assertEqual(failure['cleanup']['tasks'][0]['rollback'],
                         {'status': 'deferred', 'reason_code': 'owned_reader_exit_unverified'})

    def test_progress_failure_after_import_still_rolls_back_and_preserves_primary(self):
        source = self.thread(provider='third-party')
        before, rebuilt = self.rows(), self.rebuilt()
        baseline = self.fake_reader()
        primary = RuntimeError('synthetic observer body')

        def progress(**values):
            if values['phase'] == 'repairing' and values['checked'] == 1:
                raise primary

        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': baseline.counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', return_value=baseline):
            with self.assertRaises(RuntimeError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress, normalize_official=False)
        self.assertIs(raised.exception, primary)
        self.assertEqual(self.rows(), before)
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['cleanup']['pending_undo_count'], 0)
        self.assertNotIn('synthetic observer body', json.dumps(failure))

    def test_persistent_host_guard_failure_after_import_defers_undo(self):
        source = self.thread(provider='third-party')
        rebuilt = self.rebuilt()
        readers = [self.fake_reader(), self.fake_reader()]
        opened_final = False

        def construct(*args):
            nonlocal opened_final
            if not readers[0]._closed:
                return readers[0]
            opened_final = True
            return readers[1]

        def guard():
            if opened_final:
                raise OSError('Desktop host worker is still running')

        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': readers[0].counts.return_value}
        with patch.object(recovery, '_require_closed', side_effect=guard), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=construct), \
                patch.object(history_rebuild, 'rollback_import') as rollback:
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        rollback.assert_not_called()
        self.assertTrue(readers[1]._closed)
        self.assertFalse(raised.exception._recovery_reopen_allowed)
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['cleanup']['tasks'][0]['rollback']['reason_code'], 'host_worker_running')
        self.assertIn(('active', 'new-item', 'reconstructed new-item'), self.rows()['thread_items'])

    def test_close_failure_retry_can_restore_import_but_cannot_auto_reopen(self):
        source = self.thread(provider='third-party')
        before, rebuilt = self.rows(), self.rebuilt()
        readers = [self.fake_reader(), self.fake_reader()]
        close_calls = 0

        def final_close():
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise OSError('History reader process group did not exit')
            readers[1]._closed = True

        readers[1].close.side_effect = final_close
        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': readers[0].counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', side_effect=readers):
            with self.assertRaises(OSError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, normalize_official=False)
        self.assertEqual(close_calls, 2)
        self.assertEqual(self.rows(), before)
        self.assertFalse(raised.exception._recovery_reopen_allowed)
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['cleanup']['pending_undo_count'], 0)
        self.assertTrue(failure['cleanup']['reader_cleanup_errors'])

    def add_repairable_reasoning(self, source):
        with source.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'reasoning',
                'id': 'legacy-reasoning-id', 'summary': [], 'encrypted_content': 'synthetic-cipher',
                'content': [{'type': 'reasoning_text', 'text': 'synthetic private reasoning'}]}}) + '\n')

    def test_downstream_failure_restores_owned_raw_afterimage_and_retains_safe_cursor(self):
        from codex_switcher import history, history_write_guard
        source = self.thread()
        self.add_repairable_reasoning(source)
        original = source.read_bytes()
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_write_guard, '_require_host_closed'), \
                patch.object(history, 'busy_reason', return_value=None), \
                patch.object(history_rebuild, 'materialize_copy', side_effect=OSError('synthetic replay failure')), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[self.fake_reader(), self.fake_reader()]):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        row = result['tasks'][0]
        self.assertEqual(result['phase'], 'partial')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(row['raw_rollback']['status'], 'completed')
        self.assertEqual(row['raw_rollback']['result']['status'], 'restored')
        self.assertEqual(row['source_restored_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(result['pending_undo_count'], 0)
        self.assertEqual(self.rows()[projection.PROJECTION_TABLE], [('active', 0, 0)])

    def test_database_undo_precedes_raw_undo(self):
        from codex_switcher import history, history_write_guard
        source = self.thread()
        self.add_repairable_reasoning(source)
        original = source.read_bytes()
        rebuilt = self.rebuilt()
        readers = [self.fake_reader(), self.fake_reader({'turns': 2, 'items': 4, 'display_sha256': 'wrong'})]

        def materialize(*args):
            return {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                    'display': {'turns': 2, 'items': 4, 'display_sha256': 'expected'}}

        actual_raw_undo = recovery.message_ids.rollback_repair

        def undo_raw(*args, **kwargs):
            self.assertNotIn(('active', 'new-item', 'reconstructed new-item'), self.rows()['thread_items'])
            return actual_raw_undo(*args, **kwargs)

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_write_guard, '_require_host_closed'), \
                patch.object(history, 'busy_reason', return_value=None), \
                patch.object(history_rebuild, 'materialize_copy', side_effect=materialize), \
                patch.object(history_rebuild, 'LocalReader', side_effect=readers), \
                patch.object(recovery.message_ids, 'rollback_repair', side_effect=undo_raw) as raw_undo:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        raw_undo.assert_called_once()
        self.assertEqual(result['tasks'][0]['rollback']['status'], 'completed')
        self.assertEqual(result['tasks'][0]['raw_rollback']['status'], 'completed')
        self.assertEqual(source.read_bytes(), original)

    def test_raw_receipt_on_skipped_repair_is_retained_and_concurrent_bytes_are_not_overwritten(self):
        from codex_switcher import history, history_write_guard
        source = self.thread()
        self.add_repairable_reasoning(source)
        original_repair = recovery.message_ids.repair_file
        after_append = None

        def repair_then_concurrent_write(*args, **kwargs):
            nonlocal after_append
            changed = original_repair(*args, **kwargs)
            with source.open('ab') as stream:
                stream.write(b'{"type":"event_msg","payload":{"type":"synthetic_concurrent"}}\n')
            after_append = source.read_bytes()
            return {'changed': 0, 'skipped': 'concurrent-write', 'undo_receipt': changed['undo_receipt']}

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_write_guard, '_require_host_closed'), \
                patch.object(history, 'busy_reason', return_value=None), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=repair_then_concurrent_write), \
                patch.object(history_rebuild, 'materialize_copy') as replay, \
                patch.object(history_rebuild, 'LocalReader', side_effect=[self.fake_reader(), self.fake_reader()]):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        replay.assert_not_called()
        row = result['tasks'][0]
        self.assertTrue(Path(row['raw_undo_receipt']).is_file())
        self.assertEqual(row['raw_rollback']['status'], 'deferred')
        self.assertEqual(row['raw_rollback']['reason_code'], 'raw_afterimage_changed')
        self.assertEqual(source.read_bytes(), after_append)
        self.assertFalse(result['reopen_allowed'])
        self.assertEqual(result['pending_undo_count'], 1)

    def test_missing_committed_database_receipt_defers_cleanup_without_reopening(self):
        source = self.thread(provider='third-party')
        rebuilt = self.rebuilt()
        baseline = self.fake_reader()
        actual_import = history_rebuild.import_missing_rows

        def import_then_lose_receipt(*args, **kwargs):
            result = actual_import(*args, **kwargs)
            Path(result['undo_receipt']).unlink()
            return result

        def progress(**values):
            if values['phase'] == 'repairing' and values['checked'] == 1:
                raise RuntimeError('synthetic observer failure')

        replay = {'source_sha256': history_rebuild.digest(source), 'history_copy': str(rebuilt),
                  'display': baseline.counts.return_value}
        with patch.object(recovery, '_require_closed'), \
                patch.object(history_rebuild, 'materialize_copy', return_value=replay), \
                patch.object(history_rebuild, 'LocalReader', return_value=baseline), \
                patch.object(history_rebuild, 'import_missing_rows', side_effect=import_then_lose_receipt), \
                patch.object(history_rebuild, 'rollback_import') as rollback:
            with self.assertRaises(RuntimeError) as raised:
                recovery.run_closed(home=self.home, binary=self.binary, progress=progress, normalize_official=False)
        rollback.assert_not_called()
        self.assertFalse(raised.exception._recovery_reopen_allowed)
        failure, _ = self.failure_receipt()
        self.assertEqual(failure['cleanup']['pending_undo_count'], 1)
        self.assertEqual(failure['cleanup']['tasks'][0]['rollback'],
                         {'status': 'deferred', 'reason_code': 'missing_database_undo_receipt'})
        self.assertIn(('active', 'new-item', 'reconstructed new-item'), self.rows()['thread_items'])

    def test_missing_raw_receipt_keeps_current_bytes_and_defers_cleanup(self):
        from codex_switcher import history, history_write_guard
        source = self.thread()
        self.add_repairable_reasoning(source)
        original_repair = recovery.message_ids.repair_file
        current_bytes = None

        def repair_then_lose_receipt(*args, **kwargs):
            nonlocal current_bytes
            result = original_repair(*args, **kwargs)
            current_bytes = source.read_bytes()
            Path(result['undo_receipt']).unlink()
            return result

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_write_guard, '_require_host_closed'), \
                patch.object(history, 'busy_reason', return_value=None), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=repair_then_lose_receipt), \
                patch.object(history_rebuild, 'materialize_copy', side_effect=OSError('synthetic replay failure')), \
                patch.object(history_rebuild, 'LocalReader', side_effect=[self.fake_reader(), self.fake_reader()]), \
                patch.object(recovery.message_ids, 'rollback_repair') as rollback:
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        rollback.assert_not_called()
        self.assertEqual(source.read_bytes(), current_bytes)
        self.assertEqual(result['tasks'][0]['raw_rollback'],
                         {'status': 'deferred', 'reason_code': 'missing_raw_undo_receipt'})
        self.assertEqual(result['pending_undo_count'], 1)
        self.assertFalse(result['reopen_allowed'])

    def test_raw_receipt_attached_to_exception_is_retained_and_restored(self):
        from codex_switcher import history, history_write_guard
        source = self.thread()
        self.add_repairable_reasoning(source)
        original = source.read_bytes()
        original_repair = recovery.message_ids.repair_file

        def repair_then_fail(*args, **kwargs):
            changed = original_repair(*args, **kwargs)
            error = OSError('synthetic post-repair failure')
            error._history_undo_receipt = changed['undo_receipt']
            raise error

        with patch.object(recovery, '_require_closed'), \
                patch.object(history_write_guard, '_require_host_closed'), \
                patch.object(history, 'busy_reason', return_value=None), \
                patch.object(recovery.message_ids, 'repair_file', side_effect=repair_then_fail), \
                patch.object(history_rebuild, 'materialize_copy') as replay, \
                patch.object(history_rebuild, 'LocalReader', side_effect=[self.fake_reader(), self.fake_reader()]):
            result = recovery.run_closed(home=self.home, binary=self.binary, normalize_official=True)
        replay.assert_not_called()
        self.assertEqual(result['tasks'][0]['raw_rollback']['status'], 'completed')
        self.assertTrue(Path(result['tasks'][0]['raw_undo_receipt']).is_file())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(result['pending_undo_count'], 0)

    def test_worker_never_reopens_when_pipeline_has_pending_undo_or_cleanup_error(self):
        for outcome in ({'phase': 'partial', 'reopen_allowed': False}, OSError('synthetic primary')):
            if isinstance(outcome, BaseException):
                outcome._recovery_reopen_allowed = False
            recovery._LOCK.acquire()
            with patch.object(recovery, '_require_closed'), \
                    patch.object(recovery.codexapp, 'find_app', return_value='/synthetic'), \
                    patch.object(recovery.codexapp, 'is_running', return_value=False), \
                    patch.object(recovery.codexapp, '_reopen') as reopen, \
                    patch.object(recovery, 'run_closed', side_effect=outcome if isinstance(outcome, BaseException) else None,
                                 return_value=outcome):
                recovery._run()
            reopen.assert_not_called()
            self.assertFalse(recovery.status()['reopened'])
            self.assertFalse(recovery.status()['reopen_allowed'])


if __name__ == "__main__":
    unittest.main()
