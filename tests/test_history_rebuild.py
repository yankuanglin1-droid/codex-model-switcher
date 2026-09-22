"""Isolated reconstruction/import tests; no installed host, real home or network."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from codex_switcher import history_rebuild as rebuild, projection


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class RebuildFixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="history-rebuild-tests-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.home / "source.jsonl"
        self.raw = (json.dumps({"type": "session_meta", "payload": {"id": "target"}}) + "\n" +
                    json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "synthetic user message"}]}}) + "\n").encode()
        self.source.write_bytes(self.raw)
        self.state = self.home / "state_5.sqlite"
        with database(self.state) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,archived INTEGER,created_at INTEGER)")
            db.executemany("INSERT INTO threads VALUES (?,?,?,?)", [
                ("target", str(self.source), 0, 1), ("other", "unrelated", 0, 2),
                ("archived", "archived-source", 1, 3)])
            db.execute("CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY, description TEXT)")
            db.execute("INSERT INTO _sqlx_migrations VALUES (1,'synthetic migration')")
            db.execute("CREATE TABLE unrelated_data(id INTEGER PRIMARY KEY,payload TEXT)")
            db.execute("INSERT INTO unrelated_data VALUES (1,'unrelated synthetic data')")
        self.history = self.home / "thread_history_1.sqlite"
        self.rebuilt = self.home / "rebuilt.sqlite"
        self.make_history(self.history)
        self.make_history(self.rebuilt, add_missing=True)

    def make_history(self, path, add_missing=False):
        with database(path) as db:
            for table in rebuild.TABLES:
                db.execute("CREATE TABLE " + table + "(thread_id TEXT,row_id TEXT,payload TEXT,"
                           "PRIMARY KEY(thread_id,row_id))")
                db.executemany("INSERT INTO " + table + " VALUES (?,?,?)", [
                    ("target", "existing", "clone conflicts" if add_missing else "preserve existing"),
                    ("other", "existing", "preserve unrelated")])
                if add_missing:
                    db.execute("INSERT INTO " + table + " VALUES ('target','missing','new synthetic content')")
                    db.execute("INSERT INTO " + table + " VALUES ('other','missing','unrelated new content')")
            db.execute("CREATE TABLE thread_history_projection_state("
                       "thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)")
            db.executemany("INSERT INTO thread_history_projection_state VALUES (?,?,?)", [
                ("target", 100000 if add_missing else 1000, 50), ("other", 2000, 70)])

    def snapshot_rows(self, path):
        with database(path) as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY thread_id,row_id").fetchall()
                    for table in rebuild.TABLES}

    def cursor(self, thread_id="target"):
        with database(self.history) as db:
            return db.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM "
                              "thread_history_projection_state WHERE thread_id=?", (thread_id,)).fetchone()


class SchemaCloneTests(RebuildFixtures):
    def test_schema_clone_copies_only_selected_active_task_and_migrations(self):
        state_copy, history_copy = self.home / "state-copy.sqlite", self.home / "history-copy.sqlite"
        state_hash, history_hash = rebuild.digest(self.state), rebuild.digest(self.history)
        rebuild._clone_schema(self.state, state_copy, "target")
        rebuild._clone_schema(self.history, history_copy, "target")
        with database(state_copy) as db:
            self.assertEqual(db.execute("SELECT id FROM threads").fetchall(), [("target",)])
            self.assertEqual(db.execute("SELECT * FROM _sqlx_migrations").fetchall(), [(1, "synthetic migration")])
            self.assertEqual(db.execute("SELECT * FROM unrelated_data").fetchall(), [])
        with database(history_copy) as db:
            for table in rebuild.TABLES + ("thread_history_projection_state",):
                self.assertEqual(db.execute("SELECT DISTINCT thread_id FROM " + table).fetchall(), [("target",)])
        self.assertEqual(rebuild.digest(self.state), state_hash)
        self.assertEqual(rebuild.digest(self.history), history_hash)
        self.assertEqual(state_copy.stat().st_mode & 0o777, 0o600)

    def test_known_timestamp_trigger_is_omitted_only_in_disposable_clone(self):
        with database(self.state) as db:
            db.execute("CREATE TRIGGER threads_created_at_ms_after_insert AFTER INSERT ON threads "
                       "BEGIN UPDATE threads SET created_at=created_at+1 WHERE id=NEW.id; END")
        copy = self.home / "timestamp-copy.sqlite"
        rebuild._clone_schema(self.state, copy, "target")
        with database(copy) as db:
            self.assertEqual(db.execute("SELECT created_at FROM threads").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall(), [])
        with database(self.state) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone(), (1,))

    def test_unknown_trigger_or_view_schema_is_rejected(self):
        for definition in ("CREATE TRIGGER unknown_cleanup AFTER INSERT ON threads BEGIN DELETE FROM threads; END",
                           "CREATE VIEW unknown_view AS SELECT * FROM threads"):
            source = self.home / ("schema-" + str(len(definition)) + ".sqlite")
            rebuild.backup_database(self.state, source)
            with database(source) as db:
                db.execute(definition)
            with self.subTest(definition=definition.split()[1]), self.assertRaises(OSError):
                rebuild._clone_schema(source, self.home / (source.stem + "-copy.sqlite"), "target")

    def test_database_snapshot_is_verified_and_never_overwritten(self):
        snapshot = self.home / "snapshot.sqlite"
        report = rebuild.backup_database(self.history, snapshot)
        self.assertEqual(report["sha256"], rebuild.digest(snapshot))
        self.assertEqual(report["bytes"], snapshot.stat().st_size)
        self.assertEqual(self.snapshot_rows(snapshot), self.snapshot_rows(self.history))
        with self.assertRaises(FileExistsError):
            rebuild.backup_database(self.history, snapshot)


class MaterializeCopyTests(RebuildFixtures):
    def setUp(self):
        super().setUp()
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                db.execute("ALTER TABLE thread_turns ADD COLUMN rollout_byte_offset INTEGER DEFAULT 0")
                db.execute("ALTER TABLE thread_turns ADD COLUMN rollout_end_byte_offset INTEGER")
                db.execute("UPDATE thread_turns SET rollout_end_byte_offset=?", (len(self.raw),))

    def fake_reader(self, behavior="append"):
        owner = self
        calls = []

        class FakeReader:
            def __init__(self, binary, home):
                self.home = Path(home)
                self.count_calls = 0
                calls.append(("init", str(binary), self.home))
                with database(self.home / owner.state.name) as db:
                    self.source = Path(db.execute("SELECT rollout_path FROM threads WHERE id='target'").fetchone()[0])
                    owner.assertEqual(db.execute("SELECT id FROM threads").fetchall(), [("target",)])
                owner.assertEqual(self.source.parent, self.home / "sessions")
                owner.assertNotEqual(self.source, owner.source)
                owner.assertEqual(self.source.read_bytes(), owner.raw)
                owner.assertFalse((self.home / "auth.json").exists())

            def rpc(self, method, params, timeout=60):
                calls.append((method, params))
                owner.assertEqual(method, "thread/resume")
                owner.assertEqual(params["modelProvider"], "openai")
                owner.assertEqual(params["sandbox"], "read-only")
                owner.assertEqual(params["approvalPolicy"], "never")
                owner.assertEqual(params["cwd"], str(self.home))
                with database(self.home / owner.history.name) as db:
                    for table in (*rebuild.TABLES, rebuild.CURSOR_TABLE):
                        owner.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone(), (0,))
                # Model official replay from raw records into empty tables.
                # Fixtures intentionally mirror raw-derived rows here; the
                # byte-offset rejection tests change the rebuilt values later.
                with database(owner.history) as original, database(self.home / owner.history.name) as db:
                    for table in rebuild.TABLES:
                        for row in original.execute("SELECT * FROM " + table + " WHERE thread_id='target'"):
                            db.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", row)
                if behavior == "truncate":
                    self.source.write_bytes(b"truncated")
                elif behavior == "replace_prefix":
                    self.source.write_bytes(b"x" + owner.raw[1:])
                elif behavior == "failure":
                    raise OSError("synthetic resume failure")
                else:
                    with self.source.open("ab") as stream:
                        stream.write(b'{"type":"turn_context","payload":{"synthetic":true}}\n')
                return {}

            def counts(self, thread_id):
                owner.assertEqual(thread_id, "target")
                calls.append(("counts", thread_id))
                self.count_calls += 1
                if behavior == "before_failure" and not any(row[0] == "thread/resume" for row in calls):
                    raise OSError("synthetic missing initial projection")
                return {"turns": 1, "items": 1, "display_sha256": "synthetic-display-digest"}

            def close(self):
                calls.append(("close",))

        return FakeReader, calls

    def test_official_resume_only_sees_private_source_and_cannot_change_originals(self):
        originals = {p: (rebuild.digest(p), p.stat().st_ino) for p in (self.source, self.state, self.history)}
        reader, calls = self.fake_reader()
        destination = self.home / "private-clone"
        with patch.object(rebuild, "LocalReader", reader):
            report = rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", destination)
        self.assertEqual([call[0] for call in calls],
                         ["init", "counts", "close", "init", "thread/resume", "counts", "close"])
        self.assertEqual(report["source_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(report["display"]["items"], 1)
        self.assertEqual(report["display_before"], {
            "turns": 1, "items": 1, "display_sha256": "synthetic-display-digest"})
        self.assertEqual(Path(report["history_copy"]).parent, destination)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((destination / "sessions" / self.source.name).stat().st_mode & 0o777, 0o600)
        with database(destination / "history-before-replay.sqlite") as db:
            self.assertEqual(db.execute("SELECT payload FROM thread_items WHERE thread_id='target'").fetchone(),
                             ("preserve existing",))
            self.assertEqual(db.execute("SELECT next_rollout_byte_offset FROM " + rebuild.CURSOR_TABLE).fetchone(),
                             (1000,))
        for path, (digest, inode) in originals.items():
            self.assertEqual(rebuild.digest(path), digest)
            self.assertEqual(path.stat().st_ino, inode)

    def test_changed_or_truncated_copy_is_rejected_and_reader_always_closed(self):
        for behavior in ("truncate", "replace_prefix", "failure"):
            reader, calls = self.fake_reader(behavior)
            with self.subTest(behavior=behavior), patch.object(rebuild, "LocalReader", reader):
                with self.assertRaises(OSError):
                    rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", self.home / behavior)
            self.assertEqual(calls[-1], ("close",))
            self.assertEqual(self.source.read_bytes(), self.raw)

    def test_read_or_resume_failure_survives_a_second_close_failure(self):
        originals = {path: (rebuild.digest(path), path.stat().st_ino)
                     for path in (self.source, self.state, self.history)}
        for stage in ("baseline-counts", "replay-rpc", "replay-counts"):
            base_reader, _ = self.fake_reader()
            primary = OSError("synthetic private read failure")
            cleanup = OSError("History reader process group did not exit")
            instances = []

            class FailingReader(base_reader):
                def __init__(self, *args):
                    super().__init__(*args)
                    self._closed = False
                    instances.append(self)
                    self.is_baseline = len(instances) == 1
                    rebuild.register_local_reader(self)

                def counts(self, thread_id):
                    if ((stage == "baseline-counts" and self.is_baseline)
                            or (stage == "replay-counts" and not self.is_baseline)):
                        raise primary
                    return super().counts(thread_id)

                def rpc(self, *args, **kwargs):
                    if stage == "replay-rpc":
                        raise primary
                    return super().rpc(*args, **kwargs)

                def close(self):
                    if (stage == "baseline-counts") == self.is_baseline:
                        raise cleanup
                    self._closed = True

            with self.subTest(stage=stage), rebuild.track_local_readers() as tracked, \
                    patch.object(rebuild, "LocalReader", FailingReader):
                with self.assertRaises(OSError) as raised:
                    rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", self.home / stage)
                self.assertIs(raised.exception, primary)
                self.assertIs(primary._recovery_cleanup_error, cleanup)
                self.assertIs(primary._recovery_owned_reader, instances[-1])
                self.assertIs(cleanup._recovery_owned_reader, instances[-1])
                self.assertIs(instances[-1]._closed, False)
                self.assertEqual(tracked, instances)
            for path, (digest, inode) in originals.items():
                self.assertEqual(rebuild.digest(path), digest)
                self.assertEqual(path.stat().st_ino, inode)

    def test_cleanup_failure_without_read_failure_cannot_return_a_reconstruction(self):
        for stage in ("baseline", "replay"):
            base_reader, _ = self.fake_reader()
            cleanup = OSError("History reader output pipe did not close")
            instances = []

            class FailingCloseReader(base_reader):
                def __init__(self, *args):
                    super().__init__(*args)
                    instances.append(self)
                    self.is_baseline = len(instances) == 1

                def close(self):
                    if (stage == "baseline") == self.is_baseline:
                        raise cleanup

            with self.subTest(stage=stage), patch.object(rebuild, "LocalReader", FailingCloseReader):
                with self.assertRaises(OSError) as raised:
                    rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", self.home / (stage + "-close-only"))
                self.assertIs(raised.exception, cleanup)
                self.assertIs(cleanup._recovery_owned_reader, instances[-1])
            self.assertEqual(self.source.read_bytes(), self.raw)

    def test_source_identity_and_archived_membership_refuse_before_starting_reader(self):
        for thread_id in ("other", "archived"):
            with self.subTest(thread_id=thread_id), patch.object(rebuild, "LocalReader") as reader:
                with self.assertRaises(OSError):
                    rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, thread_id, self.home / thread_id)
                reader.assert_not_called()
        with database(self.state) as db:
            db.execute("UPDATE threads SET archived=1 WHERE id='target'")
        with patch.object(rebuild, "LocalReader") as reader, self.assertRaises(OSError):
            rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                     self.source, "target", self.home / "now-archived")
        reader.assert_not_called()

    def test_missing_initial_projection_is_reported_and_does_not_block_private_replay(self):
        reader, calls = self.fake_reader("before_failure")
        with patch.object(rebuild, "LocalReader", reader):
            report = rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", self.home / "before-unavailable")
        self.assertEqual(report["display_before"], {"error": "OSError"})
        self.assertEqual(report["display"]["items"], 1)
        self.assertEqual([call[0] for call in calls],
                         ["init", "counts", "close", "init", "thread/resume", "counts", "close"])
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_reconstructed_turn_offsets_cannot_point_into_appended_clone_tail(self):
        original_history = rebuild.digest(self.history)
        for field, value in (("rollout_byte_offset", -1), ("rollout_end_byte_offset", -1),
                             ("rollout_byte_offset", len(self.raw) + 1),
                             ("rollout_end_byte_offset", len(self.raw) + 1),
                             ("rollout_byte_offset", None), ("rollout_end_byte_offset", 1.5)):
            base_reader, calls = self.fake_reader()

            class BadOffsetReader(base_reader):
                def rpc(self, *args, **kwargs):
                    result = super().rpc(*args, **kwargs)
                    with database(self.home / self_outer.history.name) as db:
                        db.execute("UPDATE thread_turns SET " + field + "=? WHERE thread_id='target'", (value,))
                    return result

            self_outer = self
            destination = self.home / ("bad-offset-" + str(len(list(self.home.iterdir()))))
            with self.subTest(field=field, value=value), patch.object(rebuild, "LocalReader", BadOffsetReader):
                with self.assertRaises(OSError):
                    rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", destination)
            self.assertEqual(calls[-1], ("close",))
            self.assertEqual(self.source.read_bytes(), self.raw)
            self.assertEqual(rebuild.digest(self.history), original_history)

    def test_unfinished_turn_nullable_end_offset_is_preserved(self):
        with database(self.history) as db:
            db.execute("UPDATE thread_turns SET rollout_end_byte_offset=NULL WHERE thread_id='target'")
        reader, _ = self.fake_reader()
        with patch.object(rebuild, "LocalReader", reader):
            report = rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                             self.source, "target", self.home / "unfinished")
        with database(report["history_copy"]) as db:
            self.assertEqual(db.execute("SELECT rollout_end_byte_offset FROM thread_turns "
                                        "WHERE thread_id='target'").fetchone(), (None,))

    def test_replay_missing_a_previous_turn_is_rejected_even_with_valid_offsets(self):
        base_reader, calls = self.fake_reader()
        history_name = self.history.name

        class MissingTurnReader(base_reader):
            def rpc(self, *args, **kwargs):
                result = super().rpc(*args, **kwargs)
                with database(self.home / history_name) as db:
                    db.execute("DELETE FROM thread_turns WHERE thread_id='target'")
                return result

        with patch.object(rebuild, "LocalReader", MissingTurnReader):
            with self.assertRaisesRegex(OSError, "missing existing turn identities"):
                rebuild.materialize_copy("synthetic-binary", self.state, self.history,
                                         self.source, "target", self.home / "missing-turn")
        self.assertEqual(calls[-1], ("close",))


class ImportMissingRowsTests(RebuildFixtures):
    def test_only_missing_target_rows_are_inserted_existing_rows_preserved_and_idempotent(self):
        before = self.snapshot_rows(self.history)
        source_digest, rebuilt_digest = rebuild.digest(self.source), rebuild.digest(self.rebuilt)
        result = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(result, {"added_turns": 1, "added_items": 1, "added_realtime_items": 1,
                                  "conflicting_items": 2})
        after = self.snapshot_rows(self.history)
        for table in rebuild.TABLES:
            self.assertTrue(all(row in after[table] for row in before[table]))
            self.assertEqual([row for row in after[table] if row[0] == "other"],
                             [row for row in before[table] if row[0] == "other"])
            self.assertIn(("target", "missing", "new synthetic content"), after[table])
            self.assertNotIn(("target", "existing", "clone conflicts"), after[table])
        self.assertEqual(self.cursor(), (0, 0))
        self.assertEqual(self.cursor("other"), (2000, 70))
        self.assertEqual(rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True),
                         {"added_turns": 0, "added_items": 0, "added_realtime_items": 0,
                          "conflicting_items": 2})
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(rebuild.digest(self.source), source_digest)
        self.assertEqual(rebuild.digest(self.rebuilt), rebuilt_digest)

    def test_import_requires_explicit_verified_shutdown_before_opening_a_database(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value), patch.object(rebuild.sqlite3, "connect") as connect:
                with self.assertRaises(OSError):
                    rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=value)
                connect.assert_not_called()

    def test_inactive_target_validation_refuses_before_database_mutation(self):
        before = self.snapshot_rows(self.history)
        validate = MagicMock(side_effect=OSError("synthetic archived target"))
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                        validate_target=validate)
        validate.assert_called_once_with()
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_changed_target_on_commit_validation_rolls_back_all_rows_and_cursor(self):
        before = self.snapshot_rows(self.history)
        validate = MagicMock(side_effect=[None, OSError("synthetic concurrent archive or source change")])
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                        validate_target=validate)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_late_schema_mismatch_rolls_back_earlier_insertions_and_cursor(self):
        before = self.snapshot_rows(self.history)
        with database(self.rebuilt) as db:
            db.execute("ALTER TABLE thread_realtime_items ADD COLUMN unexpected TEXT")
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_late_constraint_failure_rolls_back_the_whole_import(self):
        before = self.snapshot_rows(self.history)
        with database(self.history) as db:
            db.execute("CREATE UNIQUE INDEX unique_payload ON thread_items(thread_id,payload)")
        with database(self.rebuilt) as db:
            db.execute("UPDATE thread_items SET payload='preserve existing' WHERE row_id='missing' AND thread_id='target'")
        with self.assertRaises(sqlite3.IntegrityError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_schema_replace_policy_cannot_implicitly_delete_existing_items(self):
        # SQLite permits REPLACE as a table-level conflict strategy. The writer
        # must override it with ABORT rather than trusting an ordinary INSERT.
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                rows = db.execute("SELECT * FROM thread_items").fetchall()
                db.execute("DROP TABLE thread_items")
                db.execute("CREATE TABLE thread_items(thread_id TEXT,row_id TEXT,payload TEXT,"
                           "PRIMARY KEY(thread_id,row_id),UNIQUE(thread_id,payload) ON CONFLICT REPLACE)")
                db.executemany("INSERT INTO thread_items VALUES (?,?,?)", rows)
        with database(self.rebuilt) as db:
            db.execute("UPDATE thread_items SET payload='preserve existing' WHERE row_id='missing' AND thread_id='target'")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(sqlite3.IntegrityError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_identity_schema_without_thread_id_in_primary_key_is_rejected(self):
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                db.execute("DROP TABLE thread_realtime_items")
                db.execute("CREATE TABLE thread_realtime_items(thread_id TEXT,row_id TEXT PRIMARY KEY,payload TEXT)")
                db.execute("INSERT INTO thread_realtime_items VALUES ('target','existing','preserve')")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_null_identity_is_refused_instead_of_creating_non_idempotent_duplicates(self):
        with database(self.rebuilt) as db:
            db.execute("INSERT INTO thread_items VALUES ('target',NULL,'ambiguous identity')")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_unknown_display_trigger_refuses_without_firing_or_losing_rows(self):
        with database(self.history) as db:
            db.execute("CREATE TRIGGER unknown_item_write AFTER INSERT ON thread_items BEGIN DELETE FROM thread_turns; END")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_cursor_delete_trigger_is_never_fired_by_import(self):
        with database(self.history) as db:
            db.execute(projection.KNOWN_DELETE_CLEANUP_SQL)
        before = self.snapshot_rows(self.history)
        result = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(result["added_realtime_items"], 1)
        after = self.snapshot_rows(self.history)
        for table in rebuild.TABLES:
            self.assertTrue(all(row in after[table] for row in before[table]))
        self.assertEqual(self.cursor(), (0, 0))

    def test_cursor_update_trigger_fails_closed(self):
        with database(self.history) as db:
            db.execute("CREATE TRIGGER unknown_cursor_update AFTER UPDATE ON thread_history_projection_state "
                       "BEGIN DELETE FROM thread_realtime_items; END")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))


class ImportUndoTests(RebuildFixtures):
    def import_with_receipt(self):
        receipt = self.home / "private-undo" / "import.json"
        report = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                              undo_path=receipt)
        self.assertEqual(report["undo_receipt"], str(receipt))
        return receipt

    def rollback(self, receipt, validate=None):
        return rebuild.rollback_import(self.history, receipt, "target", host_closed=True,
                                        validate_target=validate or MagicMock())

    def test_import_undo_restores_only_inserted_rows_and_cursor_and_is_repeatable(self):
        with database(self.history) as db:
            db.execute(projection.KNOWN_DELETE_CLEANUP_SQL)
        before = self.snapshot_rows(self.history)
        receipt = self.import_with_receipt()
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        raw_receipt = receipt.read_text()
        self.assertNotIn("preserve existing", raw_receipt)
        self.assertNotIn("new synthetic content", raw_receipt)
        validate = MagicMock()
        result = self.rollback(receipt, validate)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result, {"rolled_back": True, "already_restored": False,
                                  "removed_rows": 3, "restored_turns": 0, "restored_cursor": 1})
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))
        second = self.rollback(receipt)
        self.assertTrue(second["already_restored"])
        self.assertFalse(second["rolled_back"])
        self.assertEqual(self.snapshot_rows(self.history), before)

    def test_durable_receipt_before_interrupted_commit_is_safe_noop(self):
        receipt = self.home / "import.json"
        before = self.snapshot_rows(self.history)
        original = rebuild._write_import_undo

        def interrupt(path, value):
            original(path, value)
            raise KeyboardInterrupt("synthetic before commit")

        with patch.object(rebuild, "_write_import_undo", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                              undo_path=receipt)
        self.assertTrue(self.rollback(receipt)["already_restored"])
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_receipt_write_failure_cannot_commit_and_does_not_overwrite_old_receipt(self):
        receipt = self.home / "existing.json"
        receipt.write_text("previous receipt")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(FileExistsError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                          undo_path=receipt)
        self.assertEqual(receipt.read_text(), "previous receipt")
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_concurrently_changed_inserted_row_refuses_all_rollback_writes(self):
        receipt = self.import_with_receipt()
        with database(self.history) as db:
            db.execute("UPDATE thread_items SET payload='concurrent content' "
                       "WHERE thread_id='target' AND row_id='missing'")
        after = self.snapshot_rows(self.history)
        with self.assertRaisesRegex(OSError, "concurrently"):
            self.rollback(receipt)
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(self.cursor(), (0, 0))

    def test_mixed_before_after_states_refuse_even_when_no_row_was_overwritten(self):
        receipt = self.import_with_receipt()
        with database(self.history) as db:
            db.execute("DELETE FROM thread_items WHERE thread_id='target' AND row_id='missing'")
        after = self.snapshot_rows(self.history)
        with self.assertRaisesRegex(OSError, "concurrently"):
            self.rollback(receipt)
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(self.cursor(), (0, 0))

    def test_unaffected_concurrent_content_and_other_tasks_are_preserved(self):
        receipt = self.import_with_receipt()
        with database(self.history) as db:
            db.execute("UPDATE thread_items SET payload='new preserved content' WHERE row_id='existing'")
            db.execute("INSERT INTO thread_items VALUES ('other','later','preserve unrelated append')")
        self.rollback(receipt)
        with database(self.history) as db:
            rows = db.execute("SELECT * FROM thread_items ORDER BY thread_id,row_id").fetchall()
        self.assertEqual(rows, [("other", "existing", "new preserved content"),
                                ("other", "later", "preserve unrelated append"),
                                ("target", "existing", "new preserved content")])

    def test_final_validation_failure_rolls_back_the_rollback_transaction(self):
        receipt = self.import_with_receipt()
        after = self.snapshot_rows(self.history)
        validate = MagicMock(side_effect=[None, OSError("target archived during rollback")])
        with self.assertRaisesRegex(OSError, "archived"):
            self.rollback(receipt, validate)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(self.cursor(), (0, 0))
        self.assertTrue(self.rollback(receipt)["rolled_back"])

    def test_unknown_trigger_or_changed_schema_refuses_before_any_deletion(self):
        receipt = self.import_with_receipt()
        with database(self.history) as db:
            db.execute("CREATE TRIGGER destructive_delete AFTER DELETE ON thread_items "
                       "BEGIN DELETE FROM thread_realtime_items; END")
        after = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            self.rollback(receipt)
        self.assertEqual(self.snapshot_rows(self.history), after)
        with database(self.history) as db:
            db.execute("DROP TRIGGER destructive_delete")
            db.execute("CREATE INDEX unexpected_schema ON thread_items(payload)")
        with self.assertRaisesRegex(OSError, "schema changed"):
            self.rollback(receipt)
        self.assertEqual(self.snapshot_rows(self.history), after)

    def test_rollback_requires_host_closed_scope_callback_and_matching_database(self):
        receipt = self.import_with_receipt()
        for kwargs in ({}, {"host_closed": True}, {"host_closed": 1, "validate_target": MagicMock()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OSError):
                rebuild.rollback_import(self.history, receipt, "target", **kwargs)
        with self.assertRaises(OSError):
            rebuild.rollback_import(self.rebuilt, receipt, "target", host_closed=True,
                                     validate_target=MagicMock())
        with self.assertRaises(OSError):
            rebuild.rollback_import(self.history, receipt, "other", host_closed=True,
                                     validate_target=MagicMock())

    def test_cursor_concurrent_progress_refuses_rollback(self):
        receipt = self.import_with_receipt()
        with database(self.history) as db:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=1 "
                       "WHERE thread_id='target'")
        after = self.snapshot_rows(self.history)
        with self.assertRaisesRegex(OSError, "concurrently"):
            self.rollback(receipt)
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(self.cursor(), (1, 0))

    def test_foreign_key_cascade_cannot_delete_preexisting_orphan_content(self):
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                db.execute("DROP TABLE thread_items")
                db.execute("CREATE TABLE thread_items(thread_id TEXT,row_id TEXT,payload TEXT,"
                           "PRIMARY KEY(thread_id,row_id),FOREIGN KEY(thread_id,row_id) "
                           "REFERENCES thread_turns(thread_id,row_id) ON DELETE CASCADE)")
                db.execute("INSERT INTO thread_items VALUES ('target','missing','original orphan content')")
        before = self.snapshot_rows(self.history)
        self.rollback(self.import_with_receipt())
        self.assertEqual(self.snapshot_rows(self.history), before)


class CompatibleMcpDefaultsTests(RebuildFixtures):
    def install_items(self, before, after, *, old_kind="mcpToolCall", new_kind="mcpToolCall", new_ordinal=1):
        for path, content, kind, ordinal in ((self.history, before, old_kind, 1),
                                             (self.rebuilt, after, new_kind, new_ordinal)):
            with database(path) as db:
                db.execute("DROP TABLE thread_items")
                db.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,"
                           "rollout_ordinal INTEGER,created_at_ms INTEGER,item_json TEXT,"
                           "item_type TEXT,updated_at_ordinal INTEGER,PRIMARY KEY(thread_id,turn_id,item_id))")
                db.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)",
                           ("target", "existing", "item", ordinal, 123, json.dumps(content), kind, 2))
        with database(self.history) as db:
            return db.execute("SELECT * FROM thread_items").fetchall()

    def test_absent_and_null_mcp_ui_defaults_are_compatible_in_both_directions_without_writes(self):
        without = {"type": "mcpToolCall", "arguments": {"flag": False}, "result": ["synthetic"]}
        with_null = dict(without, mcpAppUi=None)
        for before, after in ((without, with_null), (with_null, without)):
            with self.subTest(before_has_default="mcpAppUi" in before):
                original = self.install_items(before, after)
                report = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
                self.assertEqual(report["compatible_defaults"], 1)
                # The unrelated synthetic realtime conflict remains a conflict.
                self.assertEqual(report["conflicting_items"], 1)
                with database(self.history) as db:
                    self.assertEqual(db.execute("SELECT * FROM thread_items").fetchall(), original)

    def test_real_field_type_other_default_and_metadata_changes_remain_conflicts(self):
        base = {"type": "mcpToolCall", "arguments": {"flag": False}}
        cases = [
            (base, {"type": "mcpToolCall", "arguments": {"flag": True}, "mcpAppUi": None}, {}),
            (base, {"type": "mcpToolCall", "arguments": {"flag": 0}, "mcpAppUi": None}, {}),
            (base, dict(base, mcpAppUi={}), {}),
            (base, dict(base, otherDefault=None), {}),
            (dict(base, mcpAppUi=None), dict(base, mcpAppUi=None, otherDefault=None), {}),
            (base, dict(base, mcpAppUi=None), {"new_ordinal": 2}),
            (base, dict(base, mcpAppUi=None), {"old_kind": "agentMessage", "new_kind": "agentMessage"}),
        ]
        for before, after, kwargs in cases:
            with self.subTest(after=after, kwargs=kwargs):
                original = self.install_items(before, after, **kwargs)
                report = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
                self.assertNotIn("compatible_defaults", report)
                self.assertEqual(report["conflicting_items"], 2)
                with database(self.history) as db:
                    self.assertEqual(db.execute("SELECT * FROM thread_items").fetchall(), original)


class KnownTurnMetadataTests(RebuildFixtures):
    def setUp(self):
        super().setUp()
        self.old_turn = ("target", "existing", 3, "inProgress", '{"old":"diagnostic"}',
                         10, None, None, "old-user", None, 100, 4, 200)
        self.fresh_turn = ("target", "existing", 5, "completed", None,
                           20, 30, 10000, "fresh-user", "fresh-agent", 120, 9, 420)
        self.other_turn = ("other", "existing", 1, "completed", None,
                           1, 2, 1000, "other-user", "other-agent", 0, 2, 100)
        self.new_turn = ("target", "missing", 10, "completed", None,
                         40, 50, 10000, "new-user", "new-agent", 420, 14, 800)
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                db.execute("DROP TABLE thread_turns")
                db.execute("CREATE TABLE thread_turns("
                           "thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,rollout_ordinal INTEGER,"
                           "status TEXT,error_json TEXT,started_at INTEGER,completed_at INTEGER,"
                           "duration_ms INTEGER,first_user_item_id TEXT,final_agent_item_id TEXT,"
                           "rollout_byte_offset INTEGER,rollout_end_ordinal INTEGER,"
                           "rollout_end_byte_offset INTEGER,PRIMARY KEY(thread_id,turn_id))")
                current = self.fresh_turn if path == self.rebuilt else self.old_turn
                db.executemany("INSERT INTO thread_turns VALUES (" + ",".join("?" * 13) + ")",
                               [current, self.other_turn])
                if path == self.rebuilt:
                    db.execute("INSERT INTO thread_turns VALUES (" + ",".join("?" * 13) + ")", self.new_turn)

    def snapshot_rows(self, path):
        with database(path) as db:
            return {table: db.execute("SELECT * FROM " + table + " ORDER BY thread_id," +
                                     ("turn_id" if table == "thread_turns" else "row_id")).fetchall()
                    for table in rebuild.TABLES}

    def test_exact_known_turn_metadata_is_refreshed_without_overwriting_item_content(self):
        original_items = self.snapshot_rows(self.history)
        source_hash, rebuilt_hash = rebuild.digest(self.source), rebuild.digest(self.rebuilt)
        report = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(report, {"added_turns": 1, "added_items": 1, "added_realtime_items": 1,
                                  "conflicting_items": 2, "updated_turns": 1})
        after = self.snapshot_rows(self.history)
        self.assertEqual(after["thread_turns"], sorted([self.fresh_turn, self.other_turn, self.new_turn]))
        for table in ("thread_items", "thread_realtime_items"):
            self.assertTrue(all(row in after[table] for row in original_items[table]))
            self.assertIn(("target", "existing", "preserve existing"), after[table])
            self.assertNotIn(("target", "existing", "clone conflicts"), after[table])
        self.assertEqual(self.cursor(), (0, 0))
        second = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(second.get("updated_turns", 0), 0)
        self.assertEqual(second["added_turns"], 0)
        self.assertEqual(self.snapshot_rows(self.history), after)
        self.assertEqual(rebuild.digest(self.source), source_hash)
        self.assertEqual(rebuild.digest(self.rebuilt), rebuilt_hash)

    def test_extra_turn_content_column_disables_metadata_overwrite(self):
        for path in (self.history, self.rebuilt):
            with database(path) as db:
                db.execute("ALTER TABLE thread_turns ADD COLUMN item_json TEXT")
                db.execute("UPDATE thread_turns SET item_json=? WHERE thread_id='target' AND turn_id='existing'",
                           ("preserve source content" if path == self.history else "different clone content",))
        before = self.snapshot_rows(self.history)["thread_turns"]
        report = rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        after = self.snapshot_rows(self.history)["thread_turns"]
        self.assertTrue(all(row in after for row in before))
        self.assertEqual(report.get("updated_turns", 0), 0)
        self.assertIn(self.old_turn + ("preserve source content",), after)
        self.assertNotIn(self.fresh_turn + ("different clone content",), after)

    def test_late_schema_failure_rolls_back_known_turn_metadata_and_new_rows(self):
        before = self.snapshot_rows(self.history)
        with database(self.rebuilt) as db:
            db.execute("ALTER TABLE thread_realtime_items ADD COLUMN unexpected TEXT")
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_commit_validation_failure_restores_old_turn_metadata(self):
        before = self.snapshot_rows(self.history)
        validate = MagicMock(side_effect=[None, OSError("synthetic concurrent archive")])
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                        validate_target=validate)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_turn_update_trigger_is_rejected_without_touching_source_content(self):
        with database(self.history) as db:
            db.execute("CREATE TRIGGER unknown_turn_update AFTER UPDATE ON thread_turns "
                       "BEGIN DELETE FROM thread_items; END")
        before = self.snapshot_rows(self.history)
        with self.assertRaises(OSError):
            rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))

    def test_undo_restores_derived_turn_metadata_without_storing_or_overwriting_items(self):
        before = self.snapshot_rows(self.history)
        receipt = self.home / "undo.json"
        rebuild.import_missing_rows(self.history, self.rebuilt, "target", host_closed=True,
                                    undo_path=receipt)
        self.assertNotIn("preserve existing", receipt.read_text())
        report = rebuild.rollback_import(self.history, receipt, "target", host_closed=True,
                                          validate_target=MagicMock())
        self.assertEqual(report["restored_turns"], 1)
        self.assertEqual(report["removed_rows"], 3)
        self.assertEqual(self.snapshot_rows(self.history), before)
        self.assertEqual(self.cursor(), (1000, 50))


class LocalReaderUnitTests(unittest.TestCase):
    def test_tracking_is_scoped_nested_and_does_not_duplicate_readers(self):
        first, second, outside = object(), object(), object()
        with rebuild.track_local_readers() as outer:
            rebuild.register_local_reader(first)
            rebuild.register_local_reader(first)
            with rebuild.track_local_readers() as inner:
                rebuild.register_local_reader(second)
            self.assertEqual(inner, [second])
            self.assertEqual(outer, [first, second])
        rebuild.register_local_reader(outside)
        with rebuild.track_local_readers() as fresh:
            self.assertEqual(fresh, [])
        self.assertEqual(outer, [first, second])

    def test_initialization_and_close_failure_retain_primary_and_owned_reader(self):
        primary = TimeoutError("Local history reader timed out")
        cleanup = OSError("History reader process group did not exit")
        process = MagicMock(pid=43210)
        with tempfile.TemporaryDirectory(prefix="reader-init-failure-") as temporary, \
                rebuild.track_local_readers() as tracked, \
                patch.object(rebuild.subprocess, "Popen", return_value=process), \
                patch.object(rebuild.threading, "Thread"), \
                patch.object(rebuild.LocalReader, "rpc", side_effect=primary), \
                patch.object(rebuild.LocalReader, "close", side_effect=cleanup):
            with self.assertRaises(TimeoutError) as raised:
                rebuild.LocalReader("never-executed-binary", temporary)
            self.assertIs(raised.exception, primary)
            self.assertIs(primary._recovery_cleanup_error, cleanup)
            self.assertEqual(len(tracked), 1)
            self.assertIs(primary._recovery_owned_reader, tracked[0])
            self.assertIs(tracked[0]._closed, False)
            self.assertEqual(tracked[0]._process_group, process.pid)

    def test_spawn_failure_records_no_owned_process_and_requires_no_signaling(self):
        failure = OSError("synthetic spawn failure")
        with tempfile.TemporaryDirectory(prefix="reader-spawn-failure-") as temporary, \
                rebuild.track_local_readers() as tracked, \
                patch.object(rebuild.subprocess, "Popen", side_effect=failure), \
                patch.object(rebuild.os, "killpg") as signal_group:
            with self.assertRaises(OSError) as raised:
                rebuild.LocalReader("never-executed-binary", temporary)
            self.assertIs(raised.exception, failure)
            self.assertEqual(len(tracked), 1)
            self.assertIs(tracked[0]._closed, True)
            self.assertIsNone(tracked[0].process)
            rebuild.close_preserving_primary(tracked[0])
            signal_group.assert_not_called()

    def test_thread_start_failure_still_closes_the_owned_group(self):
        primary = RuntimeError("synthetic thread start failure")
        thread = MagicMock()
        thread.start.side_effect = primary
        thread.ident = None
        thread.is_alive.return_value = False
        with tempfile.TemporaryDirectory(prefix="reader-thread-failure-") as temporary, \
                rebuild.track_local_readers() as tracked, \
                patch.object(rebuild.subprocess, "Popen", return_value=MagicMock(pid=43210)), \
                patch.object(rebuild.threading, "Thread", return_value=thread), \
                patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg", side_effect=ProcessLookupError) as signal_group:
            with self.assertRaises(RuntimeError) as raised:
                rebuild.LocalReader("never-executed-binary", temporary)
            self.assertIs(raised.exception, primary)
            self.assertIs(tracked[0]._closed, True)
            self.assertFalse(hasattr(primary, "_recovery_cleanup_error"))
            thread.join.assert_not_called()
            self.assertEqual(signal_group.call_args_list,
                             [call(43210, signal.SIGTERM), call(43210, 0)])

    def reader(self, pages):
        reader = object.__new__(rebuild.LocalReader)
        reader.rpc = MagicMock(side_effect=pages)
        return reader

    def test_paginated_counts_request_every_page_and_hash_display_content(self):
        turns = [{"id": "turn-a", "items": [{"id": "item-a"}]},
                 {"id": "turn-b", "items": [{"id": "item-b"}, {"id": "item-c"}]}]
        reader = self.reader([{"data": [turns[0]], "nextCursor": "next"},
                              {"data": [turns[1]], "nextCursor": None}])
        report = reader.counts("target")
        self.assertEqual((report["turns"], report["items"]), (2, 3))
        digest = hashlib.sha256()
        for turn in turns:
            digest.update(json.dumps(turn, sort_keys=True, ensure_ascii=False).encode())
        self.assertEqual(report["display_sha256"], digest.hexdigest())
        self.assertNotIn("cursor", reader.rpc.call_args_list[0].args[1])
        self.assertEqual(reader.rpc.call_args_list[1].args[1]["cursor"], "next")
        for call in reader.rpc.call_args_list:
            self.assertEqual(call.args[0], "thread/turns/list")
            self.assertEqual(call.args[1]["itemsView"], "full")

    def test_pagination_cursor_loops_abort_instead_of_hanging(self):
        for cursors in (("loop", "loop"), ("a", "b", "a")):
            reader = self.reader([{"data": [], "nextCursor": cursor} for cursor in cursors])
            with self.subTest(cursors=cursors), self.assertRaises(OSError):
                reader.counts("target")
            self.assertEqual(reader.rpc.call_count, len(cursors))

    def test_non_read_methods_are_rejected_before_any_rpc_is_sent(self):
        reader = object.__new__(rebuild.LocalReader)
        for method in ("turn/start", "auth/login", "command/exec", "thread/archive", "thread/unarchive"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                reader.rpc(method, {})

    def owned_reader(self, group=43210):
        reader = object.__new__(rebuild.LocalReader)
        reader._process_group = group
        reader._closed = False
        reader.process = MagicMock(pid=group)
        reader.process.poll.return_value = 0
        reader._reader_thread = MagicMock()
        reader._reader_thread.is_alive.return_value = False
        return reader

    def test_reader_launches_an_owned_group_and_uses_only_its_isolated_home(self):
        process = MagicMock(pid=43210)
        thread = MagicMock()
        thread.is_alive.return_value = False
        with tempfile.TemporaryDirectory(prefix="reader-group-fixture-") as temporary, \
                patch.dict(os.environ, {"SAFE_TEST": "synthetic", "OPENAI_API_KEY": "synthetic"}, clear=True), \
                patch.object(rebuild.subprocess, "Popen", return_value=process) as popen, \
                patch.object(rebuild.threading, "Thread", return_value=thread), \
                patch.object(rebuild.LocalReader, "rpc", return_value={}), \
                patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg", side_effect=ProcessLookupError):
            reader = rebuild.LocalReader("synthetic-binary", temporary)
            self.assertIs(popen.call_args.kwargs["start_new_session"], True)
            self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], str(Path(temporary).resolve()))
            self.assertNotIn("OPENAI_API_KEY", popen.call_args.kwargs["env"])
            self.assertEqual(reader._process_group, process.pid)
            reader.close()

    def test_group_wait_includes_helpers_after_direct_child_has_exited(self):
        reader = self.owned_reader()
        with patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg", side_effect=[None, ProcessLookupError]) as killpg, \
                patch.object(rebuild.time, "monotonic", side_effect=[0, 0]), \
                patch.object(rebuild.time, "sleep"):
            self.assertTrue(reader._wait_group(5))
        self.assertEqual(killpg.call_args_list, [call(43210, 0), call(43210, 0)])
        self.assertEqual(reader.process.poll.call_count, 2)

    def test_close_escalates_only_the_group_it_created_and_waits_for_exit(self):
        reader = self.owned_reader()
        with patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg") as killpg, \
                patch.object(reader, "_wait_group", side_effect=[False, True]) as wait:
            reader.close()
            reader.close()  # No signal can be sent after a verified close.
        self.assertEqual(killpg.call_args_list, [call(43210, signal.SIGTERM), call(43210, signal.SIGKILL)])
        self.assertEqual(wait.call_args_list, [call(5), call(5)])
        reader.process.terminate.assert_not_called()
        reader.process.kill.assert_not_called()
        reader.process.stdin.close.assert_called_once_with()
        reader.process.stdout.close.assert_called_once_with()

    def test_close_refuses_parent_group_and_never_claims_a_live_group_closed(self):
        reader = self.owned_reader(group=111)
        with patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg") as killpg, self.assertRaises(OSError):
            reader.close()
        killpg.assert_not_called()
        reader = self.owned_reader()
        with patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg"), \
                patch.object(reader, "_wait_group", side_effect=[False, False]), self.assertRaises(OSError):
            reader.close()
        self.assertFalse(reader._closed)

    def test_unclosed_output_pipe_blocks_success_after_group_exit(self):
        reader = self.owned_reader()
        reader._reader_thread.is_alive.return_value = True
        with patch.object(rebuild.os, "getpgrp", return_value=111), \
                patch.object(rebuild.os, "killpg", side_effect=ProcessLookupError), self.assertRaises(OSError):
            reader.close()
        self.assertFalse(reader._closed)
        reader.process.stdout.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
