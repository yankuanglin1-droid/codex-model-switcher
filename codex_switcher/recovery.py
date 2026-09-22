"""Explicit, offline recovery of active nonarchived tasks; never ordinary restart."""
from __future__ import annotations
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import sqlite3
import threading
import time
import traceback
from . import codexapp, history_rebuild, message_ids, paths, projection, platform_compat

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_CANCEL_WAIT = threading.Event()
BUSY_PHASES = ('awaiting-exit', 'quitting', 'snapshotting', 'repairing', 'verifying', 'reopening')
_STATE = dict(phase='idle', checked=0, changed=0, skipped=0, failed=0,
              rebuilt=0, added_items=0, added_turns=0)
_PLAINTEXT_REASONING_REJECTION = 'Official reasoning repair requires retained encrypted_content'
_INVALID_SOURCE_CODES = {'missing_source', 'unreadable_source', 'invalid_source_header',
                         'source_identity_mismatch', 'source_path_mismatch'}
_FAILURE_REASONS = {
    'A new user turn started; deferred recovery cancelled': 'wait_cancelled_by_followup',
    'Required turn was interrupted; recovery cancelled': 'wait_required_turn_interrupted',
    'Required task is no longer in active scope': 'wait_required_task_inactive',
    'Task history path changed while waiting': 'wait_source_path_changed',
    'Task history was replaced or truncated while waiting': 'wait_source_replaced_or_truncated',
    'Task history changed without an append': 'wait_source_changed_without_append',
    'Task lifecycle identity is ambiguous': 'wait_lifecycle_ambiguous',
    'A running task left the authorized active scope': 'wait_running_task_inactive',
    'Recovery requires a verified closed desktop host': 'host_not_closed',
    'Cannot verify whether ChatGPT/Codex has exited': 'host_exit_unverifiable',
    'Automatic storage shutdown verification requires macOS': 'unsupported_shutdown_platform',
    'Cannot verify remaining host processes': 'process_inventory_unavailable',
    'Unexpected process inventory': 'process_inventory_invalid',
    'Cannot identify desktop host installation': 'host_installation_unidentified',
    'Desktop host worker is still running': 'host_worker_running',
    'Cannot verify history storage handles': 'storage_handles_unavailable',
    'Unexpected history handle inventory': 'storage_handles_invalid',
    'Another process still has history storage open': 'storage_still_open',
    'History database changed during shutdown verification': 'storage_database_changed',
    'History storage changed during shutdown verification': 'storage_sidecar_churn',
    'Unsupported task metadata schema': 'task_metadata_schema_unsupported',
    'Raw history type is not a string': 'raw_type_invalid',
    'Raw history contains an unreadable JSONL record': 'raw_jsonl_unreadable',
    'Raw history record is not an object': 'raw_record_not_object',
    'Compacted history cannot be counted safely': 'compacted_history_invalid',
    'Raw history changed while recording structural counts': 'raw_changed_during_count',
    'Source changed before raw backup': 'source_changed_before_backup',
    'Source changed while making raw backup': 'source_changed_during_backup',
    'Raw backup readback or source verification failed': 'raw_backup_verification_failed',
    'Raw backup changed during structural verification': 'raw_backup_changed_during_verification',
    'Raw backup or original source changed before recovery': 'source_or_backup_changed_before_recovery',
    'Active task membership or metadata changed during backup': 'active_membership_changed_during_backup',
    'Source eligibility changed during backup': 'source_eligibility_changed_during_backup',
    'Source changed during final hash verification': 'source_changed_during_final_hash',
    'Raw history record or item type counts changed': 'raw_structural_counts_changed',
    'Recovery home does not match CODEX_HOME': 'recovery_home_mismatch',
    'History layout requires review before recovery': 'history_layout_requires_review',
    'Installed history reader not found': 'history_reader_missing',
    'Recovery preservation directory cannot be a symlink': 'preservation_symlink_refused',
    'Recovery snapshot verification failed': 'database_snapshot_verification_failed',
    'Local history reader requires isolated process-group support': 'reader_process_groups_unsupported',
    'Local history reader exited': 'history_reader_exited',
    'Local history reader timed out': 'history_reader_timeout',
    'History pagination did not advance': 'history_pagination_stalled',
    'Refusing to signal an unverified history reader process group': 'reader_group_unverified',
    'History reader process group did not exit': 'reader_group_still_running',
    'History reader output pipe did not close': 'reader_output_pipe_still_open',
    'History reader shutdown was not verified': 'owned_reader_exit_unverified',
    'Source changed before targeted recovery rollback': 'source_changed_before_rollback',
    'Imported history changed concurrently; rollback cancelled': 'import_afterimage_changed',
    'Raw history changed after repair; rollback deferred': 'raw_afterimage_changed',
    'Raw repair backup changed before rollback': 'raw_undo_backup_changed',
    'Raw history changed during rollback verification': 'raw_changed_during_rollback_verification',
    'Raw history changed before rollback commit': 'raw_changed_before_rollback_commit',
    'Raw history rollback readback failed': 'raw_rollback_readback_failed',
    **{'Local history reader rejected ' + method: 'reader_rejected_' + method.replace('/', '_')
       for method in ('initialize', 'thread/resume', 'thread/turns/list', 'thread/read')},
}
_PROJECTION_FAILURE_CODES = _INVALID_SOURCE_CODES | {
    'missing_state_db', 'unknown_schema', 'inactive_thread', 'unsafe_backup', 'host_not_closed',
    'invalid_targets', 'invalid_cursor_row', 'source_changed', 'cursor_changed', 'database_changed',
    'database_locked', 'sqlite_error', 'io_error',
}


def _failure_details(error):
    """Classify only known static messages; never persist exception bodies."""
    reason = None
    if len(error.args) == 1 and type(error.args[0]) is str:
        reason = _FAILURE_REASONS.get(error.args[0])
    if isinstance(error, projection.ProjectionError):
        code = getattr(error, 'code', None)
        reason = 'projection_' + code if code in _PROJECTION_FAILURE_CODES else 'projection_unclassified'
    if reason is None and isinstance(error, OSError):
        reason = {
            errno.EACCES: 'filesystem_permission_denied', errno.EPERM: 'filesystem_permission_denied',
            errno.ENOENT: 'filesystem_path_missing', errno.ENOSPC: 'filesystem_no_space',
            errno.EEXIST: 'filesystem_target_exists', errno.EIO: 'filesystem_io_error',
        }.get(error.errno, 'unclassified_os_error')
    if reason is None:
        reason = 'unclassified_sqlite_error' if isinstance(error, sqlite3.Error) else 'unclassified_exception'
    details = {'error_type': type(error).__name__, 'reason_code': reason}
    if isinstance(error, OSError) and type(error.errno) is int:
        details['errno'] = error.errno
    if isinstance(error, sqlite3.Error) and type(getattr(error, 'sqlite_errorcode', None)) is int:
        details['sqlite_errorcode'] = error.sqlite_errorcode
    return details


def _open_reader(binary, home):
    reader = history_rebuild.LocalReader(binary, home)
    # LocalReader registers itself before launching. Explicit registration also
    # keeps alternative readers/test doubles inside the same ownership scope.
    history_rebuild.register_local_reader(reader)
    return reader


def _require_readers_closed(transaction):
    if any(getattr(reader, '_closed', False) is not True for reader in transaction.get('readers', ())):
        raise OSError('History reader shutdown was not verified')


def _remember_cleanup_error(transaction, error):
    cleanup_error = getattr(error, '_recovery_cleanup_error', None)
    if not isinstance(cleanup_error, BaseException):
        cleanup_error = getattr(error, '_history_rollback_error', None)
    if not isinstance(cleanup_error, BaseException) and getattr(error, '_recovery_owned_reader', None) is not None:
        cleanup_error = error
    if isinstance(cleanup_error, BaseException):
        transaction.setdefault('cleanup_errors', []).append(_failure_details(cleanup_error))


def _rollback_unverified(transaction, *, primary_error=None):
    """Restore only this run's unverified changes, after proving reader exit."""
    report = transaction.get('report')
    result = {'phase': 'rollback-complete', 'tasks': [], 'pending_undo_count': 0,
              'reader_cleanup_errors': list(transaction.get('cleanup_errors', ())),
              'readers_closed': True, 'reopen_allowed': True}
    if isinstance(getattr(primary_error, '_recovery_cleanup_error', None), BaseException):
        result['reader_cleanup_errors'].append(_failure_details(primary_error._recovery_cleanup_error))
    elif (getattr(primary_error, '_recovery_owned_reader', None) is not None
          or getattr(primary_error, '_recovery_failure_stage', None) == 'baseline-close'):
        result['reader_cleanup_errors'].append(_failure_details(primary_error))
    for reader in transaction.get('readers', ()):
        if getattr(reader, '_closed', False) is not True:
            try:
                history_rebuild.close_preserving_primary(reader)
            except BaseException as error:
                result['reader_cleanup_errors'].append(_failure_details(error))
        if getattr(reader, '_closed', False) is not True:
            result['readers_closed'] = False
    if report is None:
        result['reopen_allowed'] = result['readers_closed'] and not result['reader_cleanup_errors']
        return result
    for row in report['tasks']:
        if row.get('recovery_verified') or not (row.get('undo_receipt') or row.get('raw_undo_receipt')):
            continue
        pending = [(receipt, outcome) for receipt, outcome in
                   (('undo_receipt', 'rollback'), ('raw_undo_receipt', 'raw_rollback'))
                   if row.get(receipt) and row.get(outcome, {}).get('status') != 'completed']
        if not pending:
            continue
        row['status'] = 'needs-review'
        task = {'thread_id': row['thread_id']}
        for name in ('undo_receipt', 'raw_undo_receipt'):
            if row.get(name):
                task[name] = row[name]
        database_restored = not row.get('undo_receipt') or row.get('rollback', {}).get('status') == 'completed'
        if not result['readers_closed']:
            for receipt, outcome in pending:
                row[outcome] = {'status': 'deferred', 'reason_code': 'owned_reader_exit_unverified'}
        else:
            tid = row['thread_id']
            if row.get('undo_receipt') and not database_restored:
                undo_path = Path(row['undo_receipt'])
                if not undo_path.is_file():
                    row['rollback'] = {'status': 'deferred', 'reason_code': 'missing_database_undo_receipt'}
                else:
                    def validate_rollback():
                        _require_closed()
                        source = transaction['active'][tid]['path']
                        projection._check_targets(transaction['home'], transaction['state_db'], [tid], {tid: source})
                        current = _source_after(transaction['home'], transaction['state_db'], tid, source)
                        if (current['sha256'] != row.get('source_sha256')
                                or current['record_counts'] != transaction['snapshots'][tid]['record_counts']):
                            raise OSError('Source changed before targeted recovery rollback')
                    try:
                        _require_closed()
                        restored = history_rebuild.rollback_import(transaction['history_db'], undo_path, tid,
                                                                    host_closed=True, validate_target=validate_rollback)
                        row['rollback'] = {'status': 'completed', 'result': restored}
                        database_restored = True
                    except BaseException as error:
                        row['rollback'] = {'status': 'deferred', **_failure_details(error)}
            if row.get('raw_undo_receipt') and row.get('raw_rollback', {}).get('status') != 'completed':
                if not database_restored:
                    row['raw_rollback'] = {'status': 'deferred', 'reason_code': 'database_rollback_incomplete'}
                elif not Path(row['raw_undo_receipt']).is_file():
                    row['raw_rollback'] = {'status': 'deferred', 'reason_code': 'missing_raw_undo_receipt'}
                else:
                    try:
                        _require_closed()
                        restored = message_ids.rollback_repair(row['raw_undo_receipt'], host_closed=True)
                        row['raw_rollback'] = {'status': 'completed', 'result': restored}
                        row['source_restored_sha256'] = restored.get('sha256')
                    except BaseException as error:
                        row['raw_rollback'] = {'status': 'deferred', **_failure_details(error)}
        for receipt, outcome in (('undo_receipt', 'rollback'), ('raw_undo_receipt', 'raw_rollback')):
            if row.get(receipt):
                task[outcome] = row[outcome]
                if row[outcome]['status'] == 'deferred':
                    result['pending_undo_count'] += 1
        result['tasks'].append(task)
    result['reopen_allowed'] = (result['readers_closed'] and not result['reader_cleanup_errors']
                                and not result['pending_undo_count'])
    if not result['reopen_allowed']:
        result['phase'] = 'rollback-deferred'
    root = report.get('backup_root')
    if root and (result['tasks'] or result['reader_cleanup_errors'] or not result['readers_closed']):
        try:
            history_rebuild.private_json(Path(root) / 'cleanup.json', result)
        except BaseException as error:
            result['cleanup_receipt_error'] = _failure_details(error)
            result['reopen_allowed'] = False
            result['phase'] = 'rollback-deferred'
    return result


def status():
    with _STATE_LOCK:
        return dict(_STATE)


def _set(**values):
    with _STATE_LOCK:
        _STATE.update(values)


def _require_closed():
    if sys.platform != 'darwin' or codexapp.is_running() is not False:
        raise OSError('Recovery requires a verified closed desktop host')
    codexapp.assert_history_idle(paths.codex_home())


def _active_rows(home, state_db):
    active = projection._active_threads(state_db, home)
    with closing(history_rebuild.connect_readonly(state_db)) as db:
        cols = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
        if not {'model_provider', 'history_mode'}.issubset(cols):
            raise OSError('Unsupported task metadata schema')
        metadata = {r[0]: r[1:] for r in db.execute(
            'SELECT id,model_provider,history_mode FROM threads WHERE archived=0')}
    return {tid: dict(path=p, provider=metadata[tid][0], mode=metadata[tid][1])
            for tid, p in active.items()}


def _raw_metrics(path):
    """Parse every JSONL record, retaining only hashes and structural counts."""
    digest = hashlib.sha256()
    counts = {'records': 0, 'blank_lines': 0, 'types': {}, 'payload_types': {},
              'type_payload_pairs': {}, 'compacted_history_types': {}}
    size = 0

    def label(value):
        if value is None:
            return '<missing>'
        if not isinstance(value, str):
            raise OSError('Raw history type is not a string')
        return value

    def increment(values, key):
        values[key] = values.get(key, 0) + 1

    with Path(path).open('rb') as stream:
        before = projection._fingerprint(os.fstat(stream.fileno()))
        for line in stream:
            digest.update(line)
            size += len(line)
            if not line.strip():
                counts['blank_lines'] += 1
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise OSError('Raw history contains an unreadable JSONL record') from exc
            if not isinstance(record, dict):
                raise OSError('Raw history record is not an object')
            kind = label(record.get('type'))
            payload = record.get('payload')
            payload_kind = label(payload.get('type')) if isinstance(payload, dict) else '<missing>'
            counts['records'] += 1
            increment(counts['types'], kind)
            increment(counts['payload_types'], payload_kind)
            increment(counts['type_payload_pairs'].setdefault(kind, {}), payload_kind)
            if kind == 'compacted' and isinstance(payload, dict):
                for field in ('replacement_history', 'guardian_history'):
                    if field not in payload:
                        continue
                    items = payload[field]
                    if items is None:
                        counts['compacted_history_types'][field] = {'<null>': 1}
                        continue
                    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                        raise OSError('Compacted history cannot be counted safely')
                    values = counts['compacted_history_types'].setdefault(field, {})
                    for item in items:
                        increment(values, label(item.get('type')))
        if (projection._fingerprint(os.fstat(stream.fileno())) != before
                or projection._fingerprint(Path(path).stat()) != before):
            raise OSError('Raw history changed while recording structural counts')
    return {'sha256': digest.hexdigest(), 'bytes': size, 'record_counts': counts}


def _snapshot_rollout(source, target, fingerprint):
    """Write an independent private raw backup and verify both source and copy."""
    source, target = Path(source), Path(target)
    value = hashlib.sha256()
    size = 0
    with source.open('rb') as original, target.open('xb') as backup:
        os.fchmod(backup.fileno(), 0o600)
        if projection._fingerprint(os.fstat(original.fileno())) != fingerprint:
            raise OSError('Source changed before raw backup')
        for block in iter(lambda: original.read(1024 * 1024), b''):
            backup.write(block)
            value.update(block)
            size += len(block)
        backup.flush()
        os.fsync(backup.fileno())
        if projection._fingerprint(os.fstat(original.fileno())) != fingerprint:
            raise OSError('Source changed while making raw backup')
    expected = value.hexdigest()
    if (projection._fingerprint(source.stat()) != fingerprint or size != fingerprint['size']
            or target.stat().st_size != size or history_rebuild.digest(target) != expected
            or history_rebuild.digest(source) != expected):
        raise OSError('Raw backup readback or source verification failed')
    metrics = _raw_metrics(target)
    if metrics['sha256'] != expected or metrics['bytes'] != size:
        raise OSError('Raw backup changed during structural verification')
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {'source': str(source), 'backup': str(target), 'bytes': size,
            'sha256': expected, 'source_fingerprint': fingerprint,
            'record_counts': metrics['record_counts']}


def _verify_raw_snapshot(home, state_db, tid, source, snapshot):
    checked = projection._check_targets(home, state_db, [tid], {tid: source})[tid]
    backup = Path(snapshot['backup'])
    if (checked['source_fingerprint'] != snapshot['source_fingerprint']
            or history_rebuild.digest(source) != snapshot['sha256']
            or backup.stat().st_size != snapshot['bytes']
            or history_rebuild.digest(backup) != snapshot['sha256']):
        raise OSError('Raw backup or original source changed before recovery')


def _snapshot_all_rollouts(home, state_db, active, root, *, progress=None):
    """Complete the whole eligible source backup set before any source/DB write."""
    directory = root / 'raw-rollouts'
    directory.mkdir(mode=0o700)
    snapshots, invalid = {}, {}
    for tid, meta in active.items():
        if meta['mode'] != 'paginated':
            continue
        _require_closed()
        try:
            checked = projection._check_targets(home, state_db, [tid], {tid: meta['path']})[tid]
        except projection.ProjectionError as exc:
            if exc.code not in _INVALID_SOURCE_CODES:
                raise
            invalid[tid] = exc.code
            continue
        # The thread identity is never used as a filesystem path component.
        target = directory / (hashlib.sha256(tid.encode('utf-8')).hexdigest() + '.jsonl')
        snapshots[tid] = _snapshot_rollout(meta['path'], target, checked['source_fingerprint'])
        history_rebuild.private_json(target.with_suffix('.json'),
                                     dict(thread_id=tid, **snapshots[tid]))
        if progress:
            progress(stage='raw-snapshot', raw_rollout_snapshot_count=len(snapshots))
    if progress:
        progress(stage='source-verification', raw_rollout_snapshot_count=len(snapshots))
    _require_closed()
    if _active_rows(home, state_db) != active:
        raise OSError('Active task membership or metadata changed during backup')
    for tid, snapshot in snapshots.items():
        _verify_raw_snapshot(home, state_db, tid, active[tid]['path'], snapshot)
    for tid, previous_code in invalid.items():
        try:
            projection._check_targets(home, state_db, [tid], {tid: active[tid]['path']})
        except projection.ProjectionError as exc:
            if exc.code == previous_code:
                continue
            raise
        raise OSError('Source eligibility changed during backup')
    return snapshots, invalid


def _source_after(home, state_db, tid, source):
    checked = projection._check_targets(home, state_db, [tid], {tid: source})[tid]
    value = _raw_metrics(source)
    if projection._fingerprint(Path(source).stat()) != checked['source_fingerprint']:
        raise OSError('Source changed during final hash verification')
    return value


def _record_source_after(row, home, state_db, source, snapshot):
    metrics = _source_after(home, state_db, row['thread_id'], source)
    row['source_after_sha256'] = metrics['sha256']
    row['source_counts_after'] = metrics['record_counts']
    if metrics['record_counts'] != snapshot['record_counts']:
        raise OSError('Raw history record or item type counts changed')
    return metrics['sha256']


def _collect_before_display(binary, home, snapshots, *, progress=None):
    """Read all real paginated baselines after backups, without resuming tasks."""
    if not snapshots:
        return {}
    if progress:
        progress(stage='baseline-open')
    _require_closed()
    try:
        reader = _open_reader(binary, home)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if (isinstance(getattr(exc, '_recovery_cleanup_error', None), BaseException)
                or (getattr(exc, '_recovery_owned_reader', None) is not None
                    and getattr(exc._recovery_owned_reader, '_closed', False) is not True)):
            exc._recovery_failure_stage = 'baseline-open'
            raise
        return {tid: {'error': type(exc).__name__} for tid in snapshots}
    before = {}
    primary_error = None
    first_read_error = None
    try:
        if progress:
            progress(stage='baseline-read')
        for tid in snapshots:
            _require_closed()
            try:
                before[tid] = reader.counts(tid)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                if first_read_error is None:
                    first_read_error = exc
                before[tid] = {'error': type(exc).__name__}
    except BaseException as error:
        primary_error = error
        error._recovery_failure_stage = 'baseline-read'
        raise
    finally:
        cleanup_error = None
        try:
            if progress:
                progress(stage='baseline-close')
        except BaseException as error:
            cleanup_error = error
        try:
            reader.close()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            if primary_error is None and first_read_error is not None:
                first_read_error._recovery_failure_stage = 'baseline-read'
                first_read_error._recovery_cleanup_error = cleanup_error
                first_read_error._recovery_owned_reader = reader
                raise first_read_error
            if primary_error is None:
                cleanup_error._recovery_failure_stage = 'baseline-close'
                raise cleanup_error
            # Preserve the original guard/read exception and traceback. Only its
            # statically classified cleanup information reaches the receipt.
            primary_error._recovery_cleanup_error = cleanup_error
    _require_closed()
    return before


def run_closed(*, home=None, binary=None, progress=None, normalize_official=False):
    with history_rebuild.track_local_readers() as readers, \
            platform_compat.file_lock(paths.state_dir() / 'recovery.lock'):
        return _run_closed_observed(home=home, binary=binary, progress=progress,
                                    normalize_official=normalize_official, transaction={'readers': readers})


def _run_closed_observed(*, home, binary, progress, normalize_official, transaction):
    observed = {'phase': 'snapshotting', 'stage': 'preflight', 'backup_count': 0}

    def observe(**values):
        observed.update(values)
        if progress:
            progress(**values)

    try:
        _require_closed()
        return _run_closed(home=home, binary=binary, progress=observe,
                           normalize_official=normalize_official, _transaction=transaction)
    except BaseException as error:
        try:
            cleanup = _rollback_unverified(transaction, primary_error=error)
        except BaseException as cleanup_error:
            cleanup = {'phase': 'rollback-deferred', 'reopen_allowed': False,
                       'cleanup_error': _failure_details(cleanup_error)}
        error._recovery_reopen_allowed = cleanup['reopen_allowed']
        stage = getattr(error, '_recovery_failure_stage', None)
        if stage not in {'baseline-open', 'baseline-read', 'baseline-close'}:
            stage = transaction.get('report', {}).get('stage', observed['stage'])
        failure = dict(schema_version=1, phase=observed['phase'], stage=stage,
                       backup_count=transaction.get('report', {}).get('backup_count', observed['backup_count']),
                       **_failure_details(error))
        failure['cleanup'] = cleanup
        failure['trace'] = [{'file': Path(frame.f_code.co_filename).name,
                             'function': frame.f_code.co_name, 'line': line}
                            for frame, line in traceback.walk_tb(error.__traceback__)]
        notification = dict(phase='error', failed_phase=failure['phase'], stage=failure['stage'],
                            backup_count=failure['backup_count'], reopen_allowed=cleanup['reopen_allowed'],
                            pending_undo_count=cleanup.get('pending_undo_count'), **_failure_details(error))
        cleanup_error = getattr(error, '_recovery_cleanup_error', None)
        if isinstance(cleanup_error, BaseException):
            failure['cleanup_error'] = _failure_details(cleanup_error)
            notification['cleanup_error'] = failure['cleanup_error']
        root = observed.get('backup_root') or transaction.get('report', {}).get('backup_root')
        if root:
            failure_path = Path(root) / 'failure.json'
            try:
                # Keep report.json at its last successfully published checkpoint.
                history_rebuild.private_json(failure_path, failure)
                notification['failure_receipt'] = str(failure_path)
            except BaseException as receipt_error:
                notification['failure_receipt_error'] = _failure_details(receipt_error)
        if progress:
            try:
                progress(**notification)
            except BaseException:
                # A broken observer must not replace the actual recovery error.
                pass
        raise


def _run_closed(*, home=None, binary=None, progress=None, normalize_official=False, _transaction=None):
    """Back up, rebuild private copies and verify, with no model inference."""
    home = Path(home or paths.codex_home()).resolve()
    if home != paths.codex_home().resolve():
        raise OSError('Recovery home does not match CODEX_HOME')
    _require_closed()
    audit = projection.audit_projection(home)
    if audit['issues'] or len(audit['history_dbs']) != 1:
        raise OSError('History layout requires review before recovery')
    state_db, history_db = Path(audit['state_db']), Path(audit['history_dbs'][0])
    binary = Path(binary or (Path(codexapp.find_app()) / 'Contents/Resources/codex'))
    if not binary.is_file():
        raise OSError('Installed history reader not found')
    preservation = home / 'model-switcher-preservation'
    if preservation.is_symlink():
        raise OSError('Recovery preservation directory cannot be a symlink')
    root = paths.ensure_dir(preservation / ('recovery-' + str(time.time_ns())))
    report = dict(phase='snapshotting', stage='database-snapshot', scope='active-nonarchived-only', tasks=[],
                  checked=0, changed=0, skipped=0, failed=0, rebuilt=0,
                  added_items=0, added_turns=0, backup_root=str(root), inference_requests=0,
                  backup_count=0, database_snapshot_count=0, raw_rollout_snapshot_count=0)
    transaction = _transaction if _transaction is not None else {'readers': []}
    transaction.update(report=report, home=home, state_db=state_db, history_db=history_db)

    def publish():
        history_rebuild.private_json(root / 'report.json', report)
        if progress:
            progress(**{k: v for k, v in report.items() if k != 'tasks'})

    publish()
    report['database_snapshots'] = {}
    for database in (state_db, history_db):
        _require_closed()
        report['database_snapshots'][database.name] = history_rebuild.backup_database(database, root / database.name)
        report['database_snapshot_count'] = len(report['database_snapshots'])
        report['backup_count'] = report['database_snapshot_count']
        publish()
    report['stage'] = 'active-selection'
    publish()
    active = _active_rows(home, state_db)
    transaction['active'] = active
    report['total'] = len(active)
    # This is a global precondition. A failure here aborts before normalization,
    # cursor updates, or imports even for tasks that were backed up successfully.
    def snapshot_progress(**values):
        report.update(values)
        report['backup_count'] = report['database_snapshot_count'] + report['raw_rollout_snapshot_count']
        publish()

    snapshot_progress(stage='raw-snapshot')
    snapshots, invalid_sources = _snapshot_all_rollouts(home, state_db, active, root,
                                                       progress=snapshot_progress)
    report['raw_rollout_snapshots'] = snapshots
    transaction['snapshots'] = snapshots
    report['raw_rollout_snapshot_count'] = len(snapshots)
    report['stage'] = 'baseline-read'
    publish()
    report['before_display'] = _collect_before_display(binary, home, snapshots,
                                                       progress=snapshot_progress)
    _require_readers_closed(transaction)
    candidates = {r['thread_id'] for r in audit['entries']
                  if r['status'] in projection.INVALID_OFFSETS or r['status'] == 'missing_cursor'}
    original_issues = {r['thread_id']: r['status'] for r in audit['entries']}
    report['phase'] = 'repairing'
    report['stage'] = 'task-recovery'
    publish()
    for tid, meta in active.items():
        _require_readers_closed(transaction)
        _require_closed()
        row = dict(thread_id=tid, status='checking', original_index=original_issues.get(tid))
        source = meta['path']
        try:
            if meta['mode'] != 'paginated':
                row['status'] = 'legacy-unchanged'
                report['skipped'] += 1
                continue
            if tid in invalid_sources:
                row.update(status='needs-review', source_issue=invalid_sources[tid])
                report['failed'] += 1
                continue
            row['raw_backup'] = snapshots[tid]['backup']
            row['source_before_sha256'] = snapshots[tid]['sha256']
            row['source_counts_before'] = snapshots[tid]['record_counts']
            row['before_display'] = report['before_display'][tid]
            _verify_raw_snapshot(home, state_db, tid, source, snapshots[tid])
            if normalize_official and meta['provider'] == 'openai':
                with source.open('rb') as stream:
                    head = json.loads(stream.readline())
                if head.get('payload', {}).get('model_provider', 'openai') != 'openai':
                    # A binding disagreement does not authorize changing the
                    # protocol; independent display recovery can still proceed.
                    row['protocol_needs_review'] = 'provider_metadata_disagreement'
                else:
                    try:
                        changes = message_ids.repair_file(source, root / 'original-rollouts', host_closed=True, official=True)
                    except BaseException as exc:
                        if getattr(exc, '_history_undo_receipt', None):
                            row['raw_undo_receipt'] = str(exc._history_undo_receipt)
                        if type(exc) is not ValueError or str(exc) != _PLAINTEXT_REASONING_REJECTION:
                            raise
                        # Only this explicit compatibility rejection can continue.
                        # Prove that the rejected writer left the backed-up source
                        # untouched before allowing independent pagination recovery.
                        _require_closed()
                        _verify_raw_snapshot(home, state_db, tid, source, snapshots[tid])
                        row['protocol_needs_review'] = 'plaintext_reasoning_requires_retained_encrypted_content'
                        candidates.add(tid)
                    else:
                        if changes.get('undo_receipt'):
                            row['raw_undo_receipt'] = changes['undo_receipt']
                        if changes.get('skipped'):
                            raise OSError('Source repair was deferred')
                        row['protocol_repair'] = changes
                        report['changed'] += changes['changed']
                        if changes['changed']:
                            candidates.add(tid)
            row['source_sha256'] = _record_source_after(row, home, state_db, source, snapshots[tid])
            if tid in candidates:
                result = history_rebuild.materialize_copy(binary, root / state_db.name, root / history_db.name,
                                                         source, tid, root / 'replay' / hashlib.sha256(tid.encode('utf-8')).hexdigest())
                _require_readers_closed(transaction)
                expected_hash = result['source_sha256']
                if 'display_before' in result:
                    row['copy_before_display'] = result['display_before']

                def validate():
                    _require_closed()
                    projection._check_targets(home, state_db, [tid], {tid: source})
                    if history_rebuild.digest(source) != expected_hash:
                        raise OSError('Source changed before import')

                undo_path = root / 'undo' / (hashlib.sha256(tid.encode('utf-8')).hexdigest() + '.json')
                # A durable undo receipt can precede COMMIT. Keep its planned
                # path even if the importer raises so cleanup can be idempotent.
                row['undo_receipt'] = str(undo_path)
                added = history_rebuild.import_missing_rows(history_db, result['history_copy'], tid,
                                                             host_closed=True, validate_target=validate,
                                                             undo_path=undo_path)
                if added.get('undo_receipt'):
                    row['undo_receipt'] = added['undo_receipt']
                row.update(replayed_display=result['display'], imported=added)
                report['rebuilt'] += 1
                report['added_items'] += added['added_items'] + added['added_realtime_items']
                report['added_turns'] += added['added_turns']
            row['status'] = 'awaiting-readback'
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            _remember_cleanup_error(transaction, exc)
            row.update(status='needs-review', error_type=type(exc).__name__)
            report['failed'] += 1
        finally:
            if tid in snapshots:
                try:
                    _record_source_after(row, home, state_db, source, snapshots[tid])
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    if row['status'] != 'needs-review':
                        report['failed'] += 1
                    row.update(status='needs-review', error_type=type(exc).__name__)
            report['checked'] += 1
            report['tasks'].append(row)
            publish()
    _require_readers_closed(transaction)
    _require_closed()
    report['phase'] = 'verifying'
    report['stage'] = 'display-readback'
    publish()
    reader = _open_reader(binary, home)
    readback_error = None
    try:
        for row in report['tasks']:
            if row['status'] != 'awaiting-readback':
                continue
            _require_closed()
            try:
                counts = reader.counts(row['thread_id'])
                row['verified_display'] = counts
                tid = row['thread_id']
                _record_source_after(row, home, state_db, active[tid]['path'], snapshots[tid])
                if row['source_after_sha256'] != row['source_sha256']:
                    raise OSError('Source changed after recovery')
                expected = row.get('replayed_display')
                before = row['before_display']
                if 'error' not in before:
                    if counts['turns'] < before['turns'] or counts['items'] < before['items']:
                        raise OSError('Display readback lost preexisting records')
                    if not expected and counts.get('display_sha256') != before.get('display_sha256'):
                        raise OSError('Untouched display contents changed during recovery')
                if row.get('imported', {}).get('conflicting_items'):
                    raise OSError('Existing and reconstructed display contents require review')
                if expected and (counts['turns'] < expected['turns'] or counts['items'] < expected['items']):
                    raise OSError('Display readback lost replayed items')
                if expected and counts.get('display_sha256') != expected.get('display_sha256'):
                    raise OSError('Display content differs from the verified reconstruction')
                row['display_status'] = 'verified'
                if row.get('protocol_needs_review'):
                    row['status'] = 'needs-review'
                    report['failed'] += 1
                else:
                    row['status'] = 'verified'
            except (OSError, ValueError, sqlite3.Error) as exc:
                row.update(status='needs-review', error_type=type(exc).__name__)
                report['failed'] += 1
            publish()
    except BaseException as error:
        readback_error = error
        raise
    finally:
        history_rebuild.close_preserving_primary(reader, readback_error)
    # Reader shutdown can append or flush delayed bytes. Completion requires a
    # final source check only after its entire process group has actually exited.
    report['stage'] = 'final-source-verification'
    publish()
    for row in report['tasks']:
        if row.get('display_status') != 'verified':
            continue
        try:
            _require_closed()
            tid = row['thread_id']
            _record_source_after(row, home, state_db, active[tid]['path'], snapshots[tid])
            if row['source_after_sha256'] != row['source_sha256']:
                raise OSError('Source changed during history reader shutdown')
            row['recovery_verified'] = True
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            if row['status'] != 'needs-review':
                report['failed'] += 1
            row.update(status='needs-review', display_status='needs-review', error_type=type(exc).__name__)
    report['stage'] = 'targeted-rollback'
    publish()
    cleanup = _rollback_unverified(transaction)
    report['pending_undo_count'] = cleanup['pending_undo_count']
    report['reopen_allowed'] = cleanup['reopen_allowed']
    if not cleanup['reopen_allowed']:
        report['failed'] = max(1, report['failed'])
    report['phase'] = 'partial' if report['failed'] else 'done'
    report['stage'] = 'complete'
    publish()
    return report


def cancel_wait():
    with _STATE_LOCK:
        if _STATE.get('phase') != 'awaiting-exit':
            return {'cancelled': False, 'reason': 'not-waiting'}
        _CANCEL_WAIT.set()
    return {'cancelled': True}


def start(*, wait_for_exit=False):
    if sys.platform != 'darwin':
        return {'error': 'Automatic recovery currently requires macOS; diagnostics remain read-only.'}
    # A live ChatGPT/Codex host owns both the WAL and an in-memory pagination
    # cache.  Never make a UI button terminate it: that used to interrupt an
    # active task and, on some desktop builds, immediately relaunch the host
    # before the repair could acquire a stable snapshot.  The online path is
    # deliberately diagnostic-only; durable rebuilds can only run when the
    # host is already closed for an independent reason.
    if codexapp.is_running():
        return {'phase': 'online-readonly', 'checked': 0, 'changed': 0,
                'reopened': False, 'history_preserved': True,
                'message': 'ChatGPT is running; history was not modified.'}
    if not _LOCK.acquire(blocking=False):
        return status()
    _CANCEL_WAIT.clear()
    _set(phase='awaiting-exit' if wait_for_exit else 'quitting', checked=0, changed=0, skipped=0, failed=0,
         rebuilt=0, added_items=0, added_turns=0, error=None, reopened=False,
         reopen_allowed=True, pending_undo_count=0)
    threading.Thread(target=_run, kwargs={'wait_for_exit': wait_for_exit},
                     daemon=True, name='history-recovery').start()
    return status()


def _run(*, wait_for_exit=False):
    closed = False
    reopen_allowed = True
    completed_phase = 'error'
    try:
        if not codexapp.find_app():
            raise OSError('Desktop application was not found')
        if wait_for_exit:
            _set(phase='awaiting-exit')
            deadline = time.monotonic() + 6 * 3600
            while codexapp.is_running():
                if _CANCEL_WAIT.wait(0.5):
                    _set(phase='cancelled')
                    return
                if time.monotonic() >= deadline:
                    _set(phase='wait-timeout')
                    return
            if _CANCEL_WAIT.is_set():
                _set(phase='cancelled')
                return
            _set(phase='quitting')
        elif codexapp.is_running():
            _set(phase='online-readonly', history_preserved=True)
            return
        _require_closed()
        closed = True
        result = run_closed(progress=_set)
        completed_phase = result['phase']
        reopen_allowed = result.get('reopen_allowed', True)
        _set(phase='reopening')
    except Exception as exc:
        reopen_allowed = getattr(exc, '_recovery_reopen_allowed',
                                 not isinstance(getattr(exc, '_recovery_cleanup_error', None), BaseException))
        _set(phase='error', error=type(exc).__name__, **_failure_details(exc))
    finally:
        if closed and reopen_allowed:
            try:
                _set(reopened=codexapp._reopen())
            except Exception:
                _set(reopened=False)
        elif closed:
            _set(reopened=False, reopen_allowed=False)
        if status()['phase'] == 'reopening':
            _set(phase=completed_phase)
        _LOCK.release()
