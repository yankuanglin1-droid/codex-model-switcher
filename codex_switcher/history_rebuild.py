"""Rebuild display indexes with the installed host, using isolated local copies.

No inference is requested. The real rollout is never passed to thread/resume;
that operation may append settings, so it only sees an expendable private copy.
Existing display records are retained when importing missing materialized rows.
"""
from __future__ import annotations

import hashlib
import base64
from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import queue
import signal
import shutil
import sqlite3
import subprocess
import threading
import time

TABLES = ("thread_turns", "thread_items", "thread_realtime_items")
KNOWN_TURN_COLUMNS = (
    "thread_id", "turn_id", "rollout_ordinal", "status", "error_json",
    "started_at", "completed_at", "duration_ms", "first_user_item_id",
    "final_agent_item_id", "rollout_byte_offset", "rollout_end_ordinal",
    "rollout_end_byte_offset",
)
READ_METHODS = {"initialize", "thread/resume", "thread/turns/list", "thread/read"}
CURSOR_TABLE = "thread_history_projection_state"
_LOCAL_READER_TRACKERS = ContextVar("history_rebuild_local_readers", default=())


@contextmanager
def track_local_readers():
    """Collect only readers created in this context for transaction cleanup.

    Nested scopes also register with their outer scopes. No process discovery or
    signaling occurs here; the returned instances own their own process groups.
    """
    readers = []
    token = _LOCAL_READER_TRACKERS.set((*_LOCAL_READER_TRACKERS.get(), readers))
    try:
        yield readers
    finally:
        _LOCAL_READER_TRACKERS.reset(token)


def register_local_reader(reader):
    """Register an owned reader; synthetic readers may use the same contract."""
    for readers in _LOCAL_READER_TRACKERS.get():
        if not any(existing is reader for existing in readers):
            readers.append(reader)


def close_preserving_primary(reader, primary_error=None):
    """Close a reader without replacing an existing operation failure.

    Cleanup failure always raises: the original operation error when supplied,
    otherwise the cleanup error. The exception attributes are memory-only;
    diagnostics must serialize static type/reason codes, never exception text,
    repr, instance attributes, or reader contents. A failed close retains the
    owned reader so a coordinator can retry and require ``_closed is True``.
    """
    try:
        reader.close()
    except BaseException as cleanup_error:
        cleanup_error._recovery_owned_reader = reader
        if primary_error is None:
            raise
        primary_error._recovery_cleanup_error = cleanup_error
        primary_error._recovery_owned_reader = reader
        raise primary_error


def _sql_name(value):
    return '"' + value.replace('"', '""') + '"'


def _encode_value(value):
    if value is None:
        return ["null", None]
    if type(value) is int:
        return ["integer", value]
    if type(value) is float:
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise OSError("Unsupported SQLite value in import receipt")


def _decode_value(value):
    if not isinstance(value, list) or len(value) != 2:
        raise OSError("Invalid import receipt value")
    kind, data = value
    if kind == "null" and data is None:
        return None
    if kind == "integer" and type(data) is int:
        return data
    if kind == "real" and isinstance(data, str):
        return float.fromhex(data)
    if kind == "text" and isinstance(data, str):
        return data
    if kind == "blob" and isinstance(data, str):
        return base64.b64decode(data, validate=True)
    raise OSError("Invalid import receipt value")


def _row_digest(row):
    encoded = [_encode_value(value) for value in row]
    return hashlib.sha256(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _undo_action(table, keys, values, before, after):
    return {"table": table, "keys": keys, "values": [_encode_value(value) for value in values],
            "before": [_encode_value(value) for value in before] if before is not None else None,
            "after_sha256": _row_digest(after)}


def _select_row(db, table, keys, values):
    rows = db.execute("SELECT * FROM " + _sql_name(table) + " WHERE " +
                      " AND ".join(_sql_name(key) + "=?" for key in keys), values).fetchall()
    if len(rows) > 1:
        raise OSError("Ambiguous history row identity")
    return rows[0] if rows else None


def _write_import_undo(path, receipt):
    """Durable before-commit evidence; never overwrite an earlier receipt."""
    path = Path(path).absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("Import receipt directory cannot be a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(receipt, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    with path.open("r", encoding="utf-8") as stream:
        if json.load(stream) != receipt:
            raise OSError("Import receipt readback failed")
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _history_definitions(db):
    placeholders = ",".join("?" for _ in (*TABLES, CURSOR_TABLE))
    return [list(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN (" +
        placeholders + ") ORDER BY type,name", (*TABLES, CURSOR_TABLE))]


def _compatible_mcp_default(columns, existing, rebuilt):
    """Recognize one reader default without ever rewriting stored item JSON."""
    if "item_type" not in columns or "item_json" not in columns:
        return False
    kind, payload = columns.index("item_type"), columns.index("item_json")
    if existing[kind] != "mcpToolCall" or rebuilt[kind] != "mcpToolCall":
        return False
    if _row_digest(existing[:payload] + existing[payload + 1:]) != _row_digest(
            rebuilt[:payload] + rebuilt[payload + 1:]):
        return False

    def invalid_constant(value):
        raise ValueError("Non-JSON numeric constant")

    try:
        before = json.loads(existing[payload], parse_constant=invalid_constant)
        after = json.loads(rebuilt[payload], parse_constant=invalid_constant)
    except (ValueError, TypeError):
        return False
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    key = "mcpAppUi"
    if (key in before) == (key in after):
        return False
    present = before if key in before else after
    if present.pop(key) is not None:
        return False
    # A canonical encoding retains JSON scalar types: false, 0 and 0.0 must
    # not accidentally compare equal as they would with Python dict equality.
    return json.dumps(before, sort_keys=True, separators=(",", ":")) == json.dumps(
        after, sort_keys=True, separators=(",", ":"))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def connect_readonly(path, *, immutable=False):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro" +
                           ("&immutable=1" if immutable else ""), uri=True)


def backup_database(source, target):
    target = Path(target)
    if target.exists():
        raise FileExistsError("Refusing to overwrite a recovery snapshot")
    with closing(connect_readonly(source)) as src, closing(sqlite3.connect(target)) as dst, dst:
        os.chmod(target, 0o600)
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise OSError("Recovery snapshot verification failed")
    return {"bytes": target.stat().st_size, "sha256": digest(target)}


def _clone_schema(source, target, thread_id):
    """Copy schema and only this task; credentials and unrelated tasks stay out."""
    with closing(connect_readonly(source, immutable=True)) as src, closing(sqlite3.connect(target)) as dst, dst:
        os.chmod(target, 0o600)
        definitions = src.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
        ).fetchall()
        for kind, name, sql in definitions:
            if kind == "trigger" and name in {
                "threads_created_at_ms_after_insert", "threads_updated_at_ms_after_insert",
                "threads_created_at_ms_after_update", "threads_updated_at_ms_after_update",
                "threads_recency_at_after_insert",
                "thread_realtime_items_projection_cleanup",
            }:
                # Display-index reconstruction does not need recency triggers.
                # Omitting them also preserves the snapshot's exact metadata.
                continue
            if kind not in ("table", "index"):
                raise OSError("Unsupported database schema in recovery copy")
            dst.execute(sql)
            if kind != "table":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in src.execute("PRAGMA table_info(" + quoted + ")")]
            if name in ("_sqlx_migrations", "thread_sections", "projects", "project_roots"):
                rows = src.execute("SELECT * FROM " + quoted)
            elif name == "threads":
                rows = src.execute("SELECT * FROM threads WHERE id=? AND archived=0", (thread_id,))
            elif name in TABLES or name in ("thread_history_projection_state", "thread_dynamic_tools"):
                rows = src.execute("SELECT * FROM " + quoted + " WHERE thread_id=?", (thread_id,))
            else:
                continue
            dst.executemany("INSERT INTO " + quoted + " VALUES (" +
                            ",".join("?" for _ in columns) + ")", rows)


class LocalReader:
    """Small stdio client. No turn/start, auth, shell or tool methods are exposed."""
    def __init__(self, binary, home):
        self._closed = True  # No owned subprocess exists until Popen succeeds.
        self.process = None
        self._process_group = None
        self._reader_thread = None
        register_local_reader(self)
        if os.name != "posix":
            raise OSError("Local history reader requires isolated process-group support")
        self.home = Path(home).resolve()
        # A replay reader owns an isolated session, not the desktop's task/IPC
        # identity. In particular, never inherit a host tools pipe or an
        # alternate SQLite home from the process that launched this repair.
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("CODEX_")
               and not any(word in k.upper() for word in ("TOKEN", "API_KEY", "SECRET", "AUTH"))}
        env["CODEX_HOME"] = str(self.home)
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "stdio://"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, start_new_session=True)
        # start_new_session makes this child the leader of a group we created.
        # Never discover or signal another process's group by executable name.
        self._closed = False
        try:
            self._process_group = self.process.pid
            self.messages = queue.Queue()
            self.number = 0
            self._reader_thread = threading.Thread(target=self._read, daemon=True)
            self._reader_thread.start()
            self.rpc("initialize", {"clientInfo": {"name": "switcher_history_recovery", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
            self.process.stdin.write('{"method":"initialized","params":{}}\n')
            self.process.stdin.flush()
        except BaseException as primary_error:
            close_preserving_primary(self, primary_error)
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                # Notifications can contain history: discard rather than log.
                if "id" in value:
                    self.messages.put(value)
        finally:
            self.messages.put(None)

    def rpc(self, method, params, timeout=60):
        if method not in READ_METHODS:
            raise ValueError("Recovery client does not allow that method")
        self.number += 1
        self.process.stdin.write(json.dumps({"id": self.number, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            try:
                value = self.messages.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError("Local history reader timed out") from None
            if value is None:
                raise OSError("Local history reader exited")
            if value.get("id") != self.number:
                continue
            if "error" in value:
                # Do not expose server error bodies: they can contain histories.
                raise OSError("Local history reader rejected " + method)
            return value["result"]

    def counts(self, thread_id):
        cursor = None
        seen = set()
        result = {"turns": 0, "items": 0}
        fingerprint = hashlib.sha256()
        while True:
            args = {"threadId": thread_id, "limit": 5, "itemsView": "full", "sortDirection": "asc"}
            if cursor:
                args["cursor"] = cursor
            page = self.rpc("thread/turns/list", args)
            for turn in page.get("data", []):
                result["turns"] += 1
                result["items"] += len(turn.get("items", []))
                fingerprint.update(json.dumps(turn, sort_keys=True, ensure_ascii=False).encode())
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise OSError("History pagination did not advance")
            seen.add(cursor)
        result["display_sha256"] = fingerprint.hexdigest()
        return result

    def _signal_group(self, sig):
        if (type(self._process_group) is not int or self._process_group <= 0
                or self._process_group == os.getpgrp()):
            raise OSError("Refusing to signal an unverified history reader process group")
        try:
            os.killpg(self._process_group, sig)
            return True
        except ProcessLookupError:
            return False

    def _wait_group(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.process.poll()  # Reap our direct child even while helpers exit.
            if not self._signal_group(0):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self):
        if self._closed:
            return
        self._signal_group(signal.SIGTERM)
        if not self._wait_group(5):
            self._signal_group(signal.SIGKILL)
            if not self._wait_group(5):
                raise OSError("History reader process group did not exit")
        if self._reader_thread is not None:
            # Thread.start can itself fail during construction; joining a thread
            # that never started would obscure the original initialization error.
            if self._reader_thread.ident is not None:
                self._reader_thread.join(timeout=1)
            if self._reader_thread.is_alive():
                raise OSError("History reader output pipe did not close")
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self._closed = True


def materialize_copy(binary, state_snapshot, history_snapshot, source, thread_id, destination):
    """Return a verified private projection; leave source and real DB untouched."""
    destination = Path(destination)
    with Path(source).open("rb") as stream:
        head = json.loads(stream.readline())
    if head.get("type") != "session_meta" or head.get("payload", {}).get("id") != thread_id:
        raise OSError("Source task identity does not match")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied = destination / "sessions" / Path(source).name
    copied.parent.mkdir(mode=0o700)
    shutil.copyfile(source, copied)
    os.chmod(copied, 0o600)
    source_hash = digest(copied)
    source_size = copied.stat().st_size
    state_copy = destination / Path(state_snapshot).name
    history_copy = destination / Path(history_snapshot).name
    _clone_schema(state_snapshot, state_copy, thread_id)
    _clone_schema(history_snapshot, history_copy, thread_id)
    with closing(sqlite3.connect(state_copy)) as db, db:
        if db.execute("UPDATE threads SET rollout_path=? WHERE id=? AND archived=0",
                      (str(copied), thread_id)).rowcount != 1:
            raise OSError("Task is no longer active")
    config = destination / "config.toml"
    config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    os.chmod(config, 0o600)
    reader = LocalReader(binary, destination)
    primary_error = None
    try:
        try:
            display_before = reader.counts(thread_id)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            primary_error = error
            display_before = {"error": type(error).__name__}
    except BaseException as error:
        primary_error = error
        raise
    finally:
        close_preserving_primary(reader, primary_error)
    # Replaying over existing rows can retain stale same-PK offsets. Preserve
    # this disposable clone's old index, then construct the canonical projection
    # from empty derived tables. The supplied snapshot and live DB stay intact.
    backup_database(history_copy, destination / "history-before-replay.sqlite")
    with closing(sqlite3.connect(history_copy)) as db, db:
        schema = db.execute("PRAGMA table_info(thread_turns)").fetchall()
        turn_keys = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
        if "thread_id" not in turn_keys:
            raise OSError("Cannot verify reconstructed turn identities")
        identity_query = "SELECT " + ",".join(_sql_name(key) for key in turn_keys) + \
                         " FROM thread_turns WHERE thread_id=?"
        previous_turns = set(db.execute(identity_query, (thread_id,)))
        for table in (*TABLES, CURSOR_TABLE):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                          (table,)).fetchone():
                raise OSError("Unexpected trigger in disposable history copy")
        for table in (*reversed(TABLES), CURSOR_TABLE):
            db.execute("DELETE FROM " + table + " WHERE thread_id=?", (thread_id,))
    reader = LocalReader(binary, destination)
    primary_error = None
    try:
        reader.rpc("thread/resume", {"threadId": thread_id, "model": "gpt-5-codex",
                   "modelProvider": "openai", "sandbox": "read-only", "approvalPolicy": "never",
                   "cwd": str(destination), "excludeTurns": True}, timeout=120)
        counts = reader.counts(thread_id)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        close_preserving_primary(reader, primary_error)
    # Resume may only append to this copy. Never accept loss of the source prefix.
    h = hashlib.sha256()
    remaining = source_size
    with copied.open("rb") as stream:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError("Recovery copy was truncated")
            h.update(block)
            remaining -= len(block)
    if h.hexdigest() != source_hash or digest(source) != source_hash:
        raise OSError("Source changed during isolated replay")
    with closing(connect_readonly(history_copy)) as db:
        reconstructed_turns = set(db.execute(identity_query, (thread_id,)))
        if not previous_turns.issubset(reconstructed_turns):
            raise OSError("Reconstructed history is missing existing turn identities")
        columns = {row[1] for row in db.execute("PRAGMA table_info(thread_turns)")}
        if not {"thread_id", "rollout_byte_offset", "rollout_end_byte_offset"}.issubset(columns):
            raise OSError("Cannot verify reconstructed turn byte offsets")
        for start, end in db.execute("SELECT rollout_byte_offset,rollout_end_byte_offset "
                                     "FROM thread_turns WHERE thread_id=?", (thread_id,)):
            if type(start) is not int or not 0 <= start <= source_size:
                raise OSError("Reconstructed turn starts outside the original source")
            if end is not None and (type(end) is not int or not 0 <= end <= source_size):
                raise OSError("Reconstructed turn ends outside the original source")
    return {"thread_id": thread_id, "source_sha256": source_hash,
            "history_copy": str(history_copy), "display_before": display_before, "display": counts}


def import_missing_rows(history_db, rebuilt_db, thread_id, *, host_closed=False,
                        validate_target=None, undo_path=None):
    """Add absent display rows and refresh only known derived turn metadata.

    Existing message/item contents and unknown turn schemas are never rewritten.
    """
    if host_closed is not True:
        raise OSError("Verified host shutdown is required")
    if validate_target:
        validate_target()
    result = {"added_turns": 0, "added_items": 0, "added_realtime_items": 0}
    conflicts = 0
    compatible_defaults = 0
    updated_turns = 0
    undo = None
    if undo_path is not None:
        database_path = Path(history_db).resolve()
        fingerprint = database_path.stat()
        undo = {"schema_version": 1, "kind": "history_import_undo", "history_db": str(database_path),
                "database_identity": [fingerprint.st_dev, fingerprint.st_ino], "thread_id": thread_id,
                "schemas": {}, "actions": []}
    with closing(sqlite3.connect(Path(history_db).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db, closing(connect_readonly(rebuilt_db)) as rebuilt:
        db.execute("BEGIN IMMEDIATE")
        from .projection import _projection_schema
        _projection_schema(db, writable=True)
        if undo is not None:
            undo["definitions"] = _history_definitions(db)
        cursor_before = _select_row(db, CURSOR_TABLE, ["thread_id"], (thread_id,))
        if undo is not None:
            undo["schemas"][CURSOR_TABLE] = [list(row) for row in db.execute(
                "PRAGMA table_info(" + CURSOR_TABLE + ")")]
        for table, count_key in zip(TABLES, result):
            schema = db.execute("PRAGMA table_info(" + table + ")").fetchall()
            if not schema or schema != rebuilt.execute("PRAGMA table_info(" + table + ")").fetchall():
                raise OSError("History schema changed; import cancelled")
            if undo is not None:
                undo["schemas"][table] = [list(row) for row in schema]
            # Avoid accepting new destructive triggers on installed databases.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchone():
                raise OSError("Unknown history trigger; import cancelled")
            keys = [r[1] for r in sorted(schema, key=lambda r: r[5]) if r[5]]
            columns = [r[1] for r in schema]
            if "thread_id" not in keys:
                raise OSError("Unknown history identity schema")
            can_refresh_turn = (table == "thread_turns" and tuple(columns) == KNOWN_TURN_COLUMNS
                                and keys == ["thread_id", "turn_id"])
            added = 0
            for row in rebuilt.execute("SELECT * FROM " + table + " WHERE thread_id=?", (thread_id,)):
                key_values = tuple(row[columns.index(k)] for k in keys)
                if any(value is None for value in key_values):
                    raise OSError("History record has an ambiguous identity")
                existing = db.execute("SELECT * FROM " + table + " WHERE " +
                                      " AND ".join(k + "=?" for k in keys), key_values).fetchone()
                if existing:
                    if can_refresh_turn and existing != row:
                        # Byte offsets and turn status/timings are derived from
                        # the authoritative rollout. No content/item_json field
                        # exists in this exact supported turn schema.
                        cursor = db.execute("UPDATE OR ABORT thread_turns SET " +
                            ",".join(column + "=?" for column in KNOWN_TURN_COLUMNS[2:]) +
                            " WHERE thread_id=? AND turn_id=?", tuple(row[2:]) + key_values)
                        if cursor.rowcount != 1:
                            raise OSError("Turn metadata identity changed during import")
                        updated_turns += 1
                        if undo is not None:
                            undo["actions"].append(_undo_action(table, keys, key_values, existing,
                                _select_row(db, table, keys, key_values)))
                    elif table != "thread_turns" and existing != row:
                        if _compatible_mcp_default(columns, existing, row):
                            compatible_defaults += 1
                        else:
                            conflicts += 1
                    continue
                db.execute("INSERT OR ABORT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", row)
                added += 1
                if undo is not None:
                    undo["actions"].append(_undo_action(table, keys, key_values, None,
                        _select_row(db, table, keys, key_values)))
            result[count_key] = added
        # Force the real host to replay its original file on the next resume.
        # Never copy the clone's cursor: its file contains an extra settings event.
        db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=0,"
                   "next_rollout_ordinal=0 WHERE thread_id=?", (thread_id,))
        if undo is not None:
            cursor_after = _select_row(db, CURSOR_TABLE, ["thread_id"], (thread_id,))
            if cursor_after != cursor_before:
                undo["actions"].append(_undo_action(CURSOR_TABLE, ["thread_id"], (thread_id,),
                                                     cursor_before, cursor_after))
        if validate_target:
            validate_target()
        if undo is not None:
            _write_import_undo(undo_path, undo)
    if conflicts:
        result["conflicting_items"] = conflicts
    if updated_turns:
        result["updated_turns"] = updated_turns
    if compatible_defaults:
        result["compatible_defaults"] = compatible_defaults
    if undo is not None:
        result["undo_receipt"] = str(Path(undo_path).absolute())
    return result


def rollback_import(history_db, undo_path, thread_id, *, host_closed=False, validate_target=None):
    """Undo only this import, refusing any affected-row or target change.

    The private receipt is durable before import commit. Thus an uncommitted
    import and a previously restored import are both safe idempotent no-ops.
    Existing item contents are neither stored in this receipt nor rewritten.
    """
    if host_closed is not True or not callable(validate_target):
        raise OSError("Verified host shutdown and active target validation are required")
    path = Path(history_db).resolve()
    identity = path.stat()
    try:
        with Path(undo_path).open("r", encoding="utf-8") as stream:
            receipt = json.load(stream)
    except (ValueError, TypeError) as error:
        raise OSError("Invalid private import receipt") from error
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("kind") != "history_import_undo"
            or receipt.get("history_db") != str(path)
            or receipt.get("database_identity") != [identity.st_dev, identity.st_ino]
            or receipt.get("thread_id") != thread_id
            or not isinstance(receipt.get("actions"), list)
            or not isinstance(receipt.get("schemas"), dict)
            or set(receipt["schemas"]) != set((*TABLES, CURSOR_TABLE))):
        raise OSError("Import receipt does not match the target database")
    result = {"rolled_back": False, "already_restored": False, "removed_rows": 0,
              "restored_turns": 0, "restored_cursor": 0}
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        # Restore the exact pre-import rows, including any preexisting orphan.
        # A cascading FK must not delete preexisting items when removing a turn
        # inserted by this import. This setting applies only to our connection.
        db.execute("PRAGMA foreign_keys=OFF")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise OSError("Cannot safely disable cascading rollback effects")
        db.execute("BEGIN IMMEDIATE")
        validate_target()
        current_identity = path.stat()
        if [current_identity.st_dev, current_identity.st_ino] != receipt["database_identity"]:
            raise OSError("History database identity changed")
        from .projection import _projection_schema
        _projection_schema(db, writable=True)
        if receipt.get("definitions") != _history_definitions(db):
            raise OSError("History schema changed since import")
        schemas = {}
        for table in (*TABLES, CURSOR_TABLE):
            schema = db.execute("PRAGMA table_info(" + table + ")").fetchall()
            if not schema or [list(row) for row in schema] != receipt["schemas"][table]:
                raise OSError("History schema changed since import")
            if table in TABLES and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchone():
                raise OSError("Unknown history trigger; rollback cancelled")
            schemas[table] = schema
        actions = []
        seen = set()
        for action in receipt["actions"]:
            if not isinstance(action, dict) or action.get("table") not in schemas:
                raise OSError("Invalid import rollback action")
            table = action["table"]
            schema = schemas[table]
            columns = [row[1] for row in schema]
            keys = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
            if ("thread_id" not in keys or action.get("keys") != keys
                    or not isinstance(action.get("values"), list)
                    or len(action["values"]) != len(keys)):
                raise OSError("Invalid import rollback identity")
            try:
                values = tuple(_decode_value(value) for value in action["values"])
                before = action.get("before")
                if before is not None:
                    before = tuple(_decode_value(value) for value in before)
            except (ValueError, TypeError) as error:
                raise OSError("Invalid import rollback values") from error
            if any(value is None for value in values) or values[keys.index("thread_id")] != thread_id:
                raise OSError("Import rollback identity is outside the target")
            identity_key = (table, _row_digest(values))
            if identity_key in seen:
                raise OSError("Duplicate import rollback identity")
            seen.add(identity_key)
            after_hash = action.get("after_sha256")
            if (not isinstance(after_hash, str) or len(after_hash) != 64
                    or any(char not in "0123456789abcdef" for char in after_hash)):
                raise OSError("Invalid import rollback row fingerprint")
            if before is not None:
                if (len(before) != len(columns)
                        or tuple(before[columns.index(key)] for key in keys) != values
                        or not (table == CURSOR_TABLE or (table == "thread_turns"
                            and tuple(columns) == KNOWN_TURN_COLUMNS
                            and keys == ["thread_id", "turn_id"]))):
                    raise OSError("Import rollback cannot overwrite existing message content")
            elif table == CURSOR_TABLE:
                raise OSError("Import rollback cannot delete a projection cursor")
            current = _select_row(db, table, keys, values)
            matches_before = (current is None if before is None else
                              current is not None and _row_digest(current) == _row_digest(before))
            matches_after = current is not None and _row_digest(current) == after_hash
            actions.append((table, keys, values, before, matches_before, matches_after))
        if all(action[4] for action in actions):
            validate_target()
            result["already_restored"] = True
            return result
        if not all(action[5] for action in actions):
            raise OSError("Imported history changed concurrently; rollback cancelled")
        for table, keys, values, before, _, _ in reversed(actions):
            where = " AND ".join(_sql_name(key) + "=?" for key in keys)
            if before is None:
                changed = db.execute("DELETE FROM " + _sql_name(table) + " WHERE " + where, values)
                result["removed_rows"] += 1
            else:
                columns = [row[1] for row in schemas[table]]
                mutable = [column for column in columns if column not in keys]
                params = tuple(before[columns.index(column)] for column in mutable) + values
                changed = db.execute("UPDATE OR ABORT " + _sql_name(table) + " SET " +
                    ",".join(_sql_name(column) + "=?" for column in mutable) + " WHERE " + where, params)
                result["restored_cursor" if table == CURSOR_TABLE else "restored_turns"] += 1
            if changed.rowcount != 1:
                raise OSError("Import rollback row identity changed")
        for table, keys, values, before, _, _ in actions:
            restored = _select_row(db, table, keys, values)
            if (restored is None) != (before is None) or (
                    before is not None and _row_digest(restored) != _row_digest(before)):
                raise OSError("Import rollback readback failed")
        validate_target()
        result["rolled_back"] = True
    return result
