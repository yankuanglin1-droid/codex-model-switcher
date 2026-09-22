"""Audit and explicitly invalidate disposable history replay cursors.

Rollout bytes and displayed turns/items/realtime records are never changed here.
The caller must verify that the host has exited before passing host_closed=True.
An audit only opens SQLite databases with mode=ro. Invalidation takes write locks,
checks live active membership and source identity again, and backs up only the
three known cursor fields before resetting both replay positions to zero. Never
delete a cursor: the host's DELETE cleanup trigger would erase realtime items.
A backup is evidence of the old rows, not of a completed reset; only returned
receipts confirm commits.

Each database transaction is atomic. Independent SQLite files cannot provide a
single atomic commit: an exceptional later commit failure reports any already
committed database paths on ProjectionError.committed_history_dbs.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path


PROJECTION_TABLE = "thread_history_projection_state"
CURSOR_FIELDS = ("thread_id", "next_rollout_byte_offset", "next_rollout_ordinal")
INVALID_OFFSETS = {"negative_offset", "beyond_eof", "not_line_boundary"}
SQLITE_TIMEOUT = 1.0
MAX_HEADER_BYTES = 1024 * 1024
KNOWN_DELETE_CLEANUP_TRIGGER = "thread_realtime_items_projection_cleanup"
KNOWN_DELETE_CLEANUP_SQL = (
    "CREATE TRIGGER thread_realtime_items_projection_cleanup "
    "AFTER DELETE ON thread_history_projection_state BEGIN "
    "DELETE FROM thread_realtime_items WHERE thread_id=OLD.thread_id; END"
)


class ProjectionError(OSError):
    """A refused/failed invalidation; no error is converted into success."""

    def __init__(self, message, *, code="unsafe_projection", backup=None,
                 committed_history_dbs=()):
        super().__init__(message)
        self.code = code
        self.backup = backup
        self.committed_history_dbs = list(committed_history_dbs)


def _versioned(home, stem):
    pattern = re.compile(re.escape(stem) + r"_(\d+)\.sqlite\Z")
    return sorted((p for p in home.glob(stem + "_*.sqlite") if pattern.fullmatch(p.name)),
                  key=lambda p: (int(pattern.fullmatch(p.name).group(1)), p.name))


def _paths(codex_home, state_db_path, history_db_paths):
    home = Path(codex_home).expanduser().absolute()
    if state_db_path is None:
        versions = _versioned(home, "state")
        state_path = versions[-1] if versions else None
    else:
        state_path = Path(state_db_path).expanduser().absolute()
    if history_db_paths is None:
        histories = _versioned(home, "thread_history")
    else:
        histories = sorted({Path(p).expanduser().absolute() for p in history_db_paths})
    return home, state_path, histories


def _connect(path, *, readonly):
    # URI encoding handles spaces, '?' and '#' in actual filesystem names.
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                                 uri=True, timeout=SQLITE_TIMEOUT, isolation_level=None)
    try:
        if readonly:
            connection.execute("PRAGMA query_only=ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _columns(connection, table):
    definition = connection.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
    if (definition is None or definition[0] != "table"
            or re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE", definition[1] or "", re.IGNORECASE)):
        raise ProjectionError("Required table is missing or unsupported: " + table,
                              code="unknown_schema")
    return {row[1]: row[2].upper().strip() for row in
            connection.execute('PRAGMA table_xinfo("' + table + '")')}


def _active_threads(state_path, home):
    if state_path is None or not state_path.is_file():
        raise ProjectionError("Active thread state database is missing", code="missing_state_db")
    connection = _connect(state_path, readonly=True)
    try:
        columns = _columns(connection, "threads")
        if any(columns.get(name) != kind for name, kind in
               {"id": "TEXT", "rollout_path": "TEXT", "archived": "INTEGER"}.items()):
            raise ProjectionError("Thread state schema cannot establish active membership",
                                  code="unknown_schema")
        rows = connection.execute("SELECT id, rollout_path FROM threads WHERE archived=0").fetchall()
        active = {}
        for thread_id, rollout_path in rows:
            if not isinstance(thread_id, str) or not thread_id or thread_id in active:
                raise ProjectionError("Active thread identity is invalid or duplicated",
                                      code="unknown_schema")
            if not isinstance(rollout_path, str) or not rollout_path:
                active[thread_id] = None
            else:
                path = Path(rollout_path).expanduser()
                active[thread_id] = (home / path if not path.is_absolute() else path).absolute()
        return active
    finally:
        connection.close()


def _projection_schema(connection, *, writable=False):
    columns = _columns(connection, PROJECTION_TABLE)
    if columns != {"thread_id": "TEXT", "next_rollout_byte_offset": "INTEGER",
                   "next_rollout_ordinal": "INTEGER"}:
        raise ProjectionError("Unsupported replay cursor schema; no rows changed", code="unknown_schema")
    if writable:
        # UPDATE never runs this known DELETE-only cleanup. Refuse every unknown
        # trigger, especially UPDATE triggers which could erase displayed items.
        known_sql = "".join(KNOWN_DELETE_CLEANUP_SQL.lower().split()).rstrip(";")
        for name, sql in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (PROJECTION_TABLE,)):
            normalized_sql = "".join((sql or "").lower().split()).rstrip(";")
            if name.lower() != KNOWN_DELETE_CLEANUP_TRIGGER or normalized_sql != known_sql:
                raise ProjectionError("Replay cursor trigger is unsupported", code="unknown_schema")
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            escaped = table.replace('"', '""')
            for fk in connection.execute('PRAGMA foreign_key_list("' + escaped + '")'):
                if fk[2].lower() == PROJECTION_TABLE and fk[5].upper() not in {"NO ACTION", "RESTRICT"}:
                    raise ProjectionError("Replay cursor update has unsupported side effects",
                                          code="unknown_schema")


def _cursor(connection, thread_id):
    rows = connection.execute(
        "SELECT thread_id,next_rollout_byte_offset,next_rollout_ordinal "
        "FROM thread_history_projection_state WHERE thread_id=?", (thread_id,)).fetchall()
    if len(rows) > 1:
        raise ProjectionError("Replay cursor identity is duplicated", code="unknown_schema")
    return dict(zip(CURSOR_FIELDS, rows[0])) if rows else None


def _fingerprint(value):
    return {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def _source(path, thread_id, offset=None):
    result = {"rollout_path": str(path) if path is not None else None,
              "source_size": None, "source_fingerprint": None}
    if path is None:
        return dict(result, status="missing_source")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return dict(result, status="unreadable_source")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return dict(result, status="unreadable_source")
            result.update(source_size=before.st_size, source_fingerprint=_fingerprint(before))
            # Bound memory usage and never place the source body in a report.
            used = 0
            while True:
                line = stream.readline(MAX_HEADER_BYTES - used + 1)
                used += len(line)
                if used > MAX_HEADER_BYTES or not line:
                    return dict(result, status="invalid_source_header")
                if line.strip():
                    break
            try:
                header = json.loads(line)
            except (ValueError, UnicodeError):
                return dict(result, status="invalid_source_header")
            if (not isinstance(header, dict) or header.get("type") != "session_meta"
                    or not isinstance(header.get("payload"), dict)
                    or header["payload"].get("id") != thread_id):
                return dict(result, status="source_identity_mismatch")
            status = "valid"
            if offset is not None:
                if type(offset) is not int:
                    status = "invalid_offset"
                elif offset < 0:
                    status = "negative_offset"
                elif offset > before.st_size:
                    status = "beyond_eof"
                elif offset > 0:
                    stream.seek(offset - 1)
                    if stream.read(1) != b"\n":
                        status = "not_line_boundary"
            if (_fingerprint(os.fstat(stream.fileno())) != result["source_fingerprint"]
                    or _fingerprint(path.stat()) != result["source_fingerprint"]):
                status = "source_changed"
            return dict(result, status=status)
    except FileNotFoundError:
        return dict(result, status="missing_source")
    except OSError:
        return dict(result, status="unreadable_source")


def _issue(exc, path):
    # Avoid logging SQLite row values, headers, titles or conversation content.
    return {"code": getattr(exc, "code", "unreadable_database"),
            "path": str(path) if path is not None else None,
            "error_type": type(exc).__name__}


def audit_projection(codex_home, *, state_db_path=None, history_db_paths=None):
    """Read only active, nonarchived threads and report cursor/source problems.

    Unknown/missing/unreadable databases are issues, never a clean audit. A zero
    cursor and any byte immediately after a newline are valid boundaries. EOF
    without a terminal newline is not a completed JSONL boundary.
    """
    home, state_path, histories = _paths(codex_home, state_db_path, history_db_paths)
    report = {"read_only": True, "state_db": str(state_path) if state_path else None,
              "history_dbs": [str(p) for p in histories], "active_threads": 0,
              "entries": [], "issues": [], "invalid_count": 0}
    try:
        active = _active_threads(state_path, home)
    except (OSError, sqlite3.Error) as exc:
        report["issues"].append(_issue(exc, state_path))
        return report
    report["active_threads"] = len(active)
    if not histories:
        report["issues"].append({"code": "missing_history_db", "path": str(home)})
    for database in histories:
        connection = None
        try:
            if not database.exists():
                report["issues"].append({"code": "missing_history_db", "path": str(database)})
                continue
            connection = _connect(database, readonly=True)
            _projection_schema(connection)
            for thread_id, source_path in sorted(active.items()):
                row = _cursor(connection, thread_id)
                source = _source(source_path, thread_id,
                                 row["next_rollout_byte_offset"] if row else None)
                if row and type(row["next_rollout_byte_offset"]) is not int:
                    source["status"] = "invalid_offset" if source["status"] == "valid" else source["status"]
                if row and (type(row["next_rollout_ordinal"]) is not int or row["next_rollout_ordinal"] < 0):
                    source["status"] = "invalid_ordinal" if source["status"] == "valid" else source["status"]
                if row is None and source["status"] == "valid":
                    source["status"] = "missing_cursor"
                report["entries"].append(dict(source, history_db=str(database), thread_id=thread_id,
                    next_rollout_byte_offset=row["next_rollout_byte_offset"] if row else None,
                    next_rollout_ordinal=row["next_rollout_ordinal"] if row else None))
        except (OSError, sqlite3.Error) as exc:
            report["issues"].append(_issue(exc, database))
        finally:
            if connection is not None:
                connection.close()
    report["invalid_count"] = sum(row["status"] in INVALID_OFFSETS for row in report["entries"])
    return report


def _check_targets(home, state_path, targets, rollout_paths):
    active = _active_threads(state_path, home)
    sources = {}
    for thread_id in targets:
        if thread_id not in active:
            raise ProjectionError("Target is not a current nonarchived thread", code="inactive_thread")
        path = active[thread_id]
        if rollout_paths is not None:
            if thread_id not in rollout_paths or path is None or path.resolve() != Path(rollout_paths[thread_id]).resolve():
                raise ProjectionError("Requested source does not match active thread state", code="source_path_mismatch")
        source = _source(path, thread_id)
        if source["status"] != "valid":
            raise ProjectionError("Active rollout source cannot be verified", code=source["status"])
        sources[thread_id] = source
    return sources


def _write_backup(home, rows):
    directory = home / "projection-backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ProjectionError("Projection backup directory cannot be a symlink", code="unsafe_backup")
    os.chmod(directory, 0o700)
    descriptor, filename = tempfile.mkstemp(prefix="projection-", suffix=".json", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"schema_version": 1, "kind": "projection_cursor_backup",
                       "operation": "reset_cursor",
                       "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "table": PROJECTION_TABLE, "columns": list(CURSOR_FIELDS),
                       "rows": rows}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        Path(filename).unlink(missing_ok=True)
        raise
    return filename


def invalidate_projection(codex_home, thread_ids, *, host_closed=False, force=False,
                          state_db_path=None, history_db_paths=None, rollout_paths=None):
    """Reset selected replay cursors to zero, preserving every displayed row.

    Only broken numeric cursors are reset unless force=True (used immediately
    before an authorized byte rewrite). Missing cursors and existing zero/zero
    cursors are idempotent successes. Cursor rows are never deleted, because the
    host's DELETE cleanup trigger would otherwise erase realtime history items.
    Unknown schemas, archived/missing targets or unverifiable sources fail closed.
    Optional rollout_paths maps IDs to the exact files an external writer intends
    to rewrite, preventing a stale or misidentified path from passing this guard.
    """
    if host_closed is not True:
        raise ProjectionError("Projection invalidation requires verified host shutdown", code="host_not_closed")
    if isinstance(thread_ids, (str, bytes)):
        raise ProjectionError("Pass an explicit sequence of thread IDs", code="invalid_targets")
    try:
        targets = list(thread_ids)
    except TypeError as exc:
        raise ProjectionError("Pass an explicit sequence of thread IDs", code="invalid_targets") from exc
    if any(not isinstance(value, str) or not value for value in targets):
        raise ProjectionError("Thread IDs must be nonempty strings", code="invalid_targets")
    targets = sorted(set(targets))
    if not targets:
        return {"invalidated": 0, "backup": None, "receipts": [], "skipped": []}
    home, state_path, histories = _paths(codex_home, state_db_path, history_db_paths)
    backup = None
    opened, committed, rows, skipped = [], [], [], []
    try:
        initial_sources = _check_targets(home, state_path, targets, rollout_paths)
        if not histories:
            skipped.extend({"history_db": None, "thread_id": thread_id,
                            "rollout_path": initial_sources[thread_id]["rollout_path"],
                            "reason": "missing_cursor"} for thread_id in targets)
        for database in histories:
            connection = _connect(database, readonly=False)
            opened.append((database, connection))
            connection.execute("BEGIN IMMEDIATE")
            _projection_schema(connection, writable=True)
            for thread_id in targets:
                row = _cursor(connection, thread_id)
                source = initial_sources[thread_id]
                context = {"history_db": str(database), "rollout_path": source["rollout_path"],
                           "thread_id": thread_id}
                if row is None:
                    skipped.append(dict(context, reason="missing_cursor"))
                    continue
                if (type(row["next_rollout_byte_offset"]) is not int
                        or type(row["next_rollout_ordinal"]) is not int or row["next_rollout_ordinal"] < 0):
                    raise ProjectionError("Replay cursor row has unsupported values", code="invalid_cursor_row")
                check = _source(Path(source["rollout_path"]), thread_id, row["next_rollout_byte_offset"])
                if check["source_fingerprint"] != source["source_fingerprint"]:
                    raise ProjectionError("Rollout changed during invalidation", code="source_changed")
                if check["status"] not in INVALID_OFFSETS | {"valid"}:
                    raise ProjectionError("Rollout cannot be verified", code=check["status"])
                if row["next_rollout_byte_offset"] == 0 and row["next_rollout_ordinal"] == 0:
                    skipped.append(dict(context, reason="already_reset"))
                    continue
                if not force and check["status"] == "valid":
                    skipped.append(dict(context, reason="valid_cursor"))
                    continue
                rows.append(dict(context, **{key: row[key] for key in CURSOR_FIELDS if key != "thread_id"},
                                 operation="reset_cursor", new_next_rollout_byte_offset=0,
                                 new_next_rollout_ordinal=0,
                                 source_fingerprint=source["source_fingerprint"]))
        # All schemas and write locks are checked before any database is changed.
        if rows:
            backup = _write_backup(home, rows)
        for database, connection in opened:
            for row in rows:
                if row["history_db"] == str(database):
                    updated = connection.execute(
                        "UPDATE thread_history_projection_state "
                        "SET next_rollout_byte_offset=0,next_rollout_ordinal=0 WHERE thread_id=? "
                        "AND next_rollout_byte_offset=? AND next_rollout_ordinal=?",
                        tuple(row[key] for key in CURSOR_FIELDS)).rowcount
                    if updated != 1:
                        raise ProjectionError("Replay cursor changed during invalidation", code="cursor_changed")
        # Reopen read-only state so a concurrent archive/path change is visible.
        _, current_state, current_histories = _paths(home, state_db_path, history_db_paths)
        if current_state != state_path or current_histories != histories:
            raise ProjectionError("History database selection changed", code="database_changed")
        if _check_targets(home, state_path, targets, rollout_paths) != initial_sources:
            raise ProjectionError("Rollout source changed during invalidation", code="source_changed")
        for database, connection in opened:
            connection.commit()
            committed.append(str(database))
        return {"invalidated": len(rows), "backup": backup, "receipts": rows, "skipped": skipped}
    except (OSError, sqlite3.Error) as exc:
        rollback_failed = False
        for database, connection in opened:
            if str(database) not in committed and connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    rollback_failed = True
        code = "rollback_failed" if rollback_failed else getattr(exc, "code", "database_error")
        raise ProjectionError("Projection invalidation failed (" + code + "); inspect backup/commit receipt",
                              code=code, backup=backup, committed_history_dbs=committed) from exc
    finally:
        for _, connection in opened:
            connection.close()
