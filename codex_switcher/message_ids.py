"""Responses item IDs and explicitly requested official-endpoint compatibility."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path


# Item IDs identify records; call_id links calls to outputs and must stay intact.
ITEM_ID_PREFIXES = {"message": "msg", "function_call": "fc", "reasoning": "rs"}


def _sync_directory(path):
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_id(info):
    return [info.st_dev, info.st_ino]


def _write_undo(path, value):
    """A durable immutable intent exists before either cursor or raw changes."""
    with Path(path).open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(Path(path).read_text(encoding="utf-8")) != value:
        raise OSError("Raw repair undo receipt readback failed")
    _sync_directory(Path(path).parent)


def rollback_repair(undo_path, *, host_closed=False):
    """Restore only this repair's exact afterimage; never overwrite new work.

    The cursor remains safely invalidated. Restoring an obsolete cursor from a
    pre-repair snapshot could reintroduce an offset beyond the original EOF.
    """
    from .history import same_snapshot
    from .history_write_guard import _require_host_closed, prepare_rewrite
    _require_host_closed(host_closed)
    undo_path = Path(undo_path)
    if undo_path.is_symlink():
        raise OSError("Raw repair receipt cannot be a symlink")
    receipt = json.loads(undo_path.read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict) or receipt.get("kind") != "raw-history-repair"
            or receipt.get("schema_version") != 1):
        raise OSError("Unsupported raw repair receipt")
    source, backup = Path(receipt["source"]), Path(receipt["backup"])
    if source.is_symlink() or backup.is_symlink():
        raise OSError("Raw repair source or backup cannot be a symlink")
    original = backup.read_bytes()
    if hashlib.sha256(original).hexdigest() != receipt["before_sha256"]:
        raise OSError("Raw repair backup changed before rollback")
    current_stat, current = source.stat(), source.read_bytes()
    if not same_snapshot(source, current_stat, current):
        raise OSError("Raw history changed during rollback verification")
    current_hash = hashlib.sha256(current).hexdigest()
    if current_hash == receipt["before_sha256"]:
        return {"status": "already-restored"}
    if (current_hash != receipt["after_sha256"]
            or _file_id(current_stat) != receipt["after_file_id"]):
        raise OSError("Raw history changed after repair; rollback deferred")
    fd, temporary = tempfile.mkstemp(dir=source.parent, prefix=".history-rollback-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, receipt["original_mode"] & 0o777)
        prepare_rewrite(source, host_closed=host_closed)
        _require_host_closed(host_closed)
        if not same_snapshot(source, current_stat, current):
            raise OSError("Raw history changed before rollback commit")
        os.replace(temporary, source)
        _sync_directory(source.parent)
        if source.read_bytes() != original:
            raise OSError("Raw history rollback readback failed")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"status": "restored", "sha256": receipt["before_sha256"]}

def _normalize_record(record, official=False):
    """Return separate item-ID and official reasoning-field change counts."""
    if not isinstance(record, dict):
        return 0, 0
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return 0, 0
    items = []
    if record.get("type") == "response_item":
        items.append(payload)
    elif record.get("type") == "compacted":
        for field in ("replacement_history", "guardian_history"):
            value = payload.get(field)
            if isinstance(value, list):
                items.extend(value)
    changed = 0
    reasoning_content_removed = 0
    if official is True:
        # Validate the whole record before mutating any item. Plaintext-only
        # reasoning must never be discarded to make an endpoint accept it.
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            encrypted = item.get("encrypted_content")
            if item.get("content") and not (isinstance(encrypted, str) and encrypted.strip()):
                raise ValueError("Official reasoning repair requires retained encrypted_content")
    for item in items:
        if not isinstance(item, dict):
            continue
        # Third-party endpoints may need this field. Only a caller that has
        # explicitly selected the official endpoint may remove it. Retain the
        # reasoning item itself, encrypted_content, summary and every other key.
        if (official is True and item.get("type") == "reasoning"
                and isinstance(item.get("content"), list) and item["content"]):
            del item["content"]
            reasoning_content_removed += 1
        prefix = ITEM_ID_PREFIXES.get(item.get("type"))
        if prefix is None:
            continue
        value = item.get("id")
        if isinstance(value, str) and value and not value.startswith(prefix):
            item["id"] = prefix + "_" + hashlib.sha256(value.encode()).hexdigest()[:32]
            changed += 1
    return changed, reasoning_content_removed


def normalize_record(record, *, official=False):
    """Normalize typed items; official reasoning compatibility requires opt-in.

    Default callers retain reasoning.content. With official=True, remove a
    nonempty content array only when encrypted_content retains the reasoning.
    Empty/null content is preserved. Never remove records or recurse into user text,
    arbitrary tool output, encrypted_content, or summary.
    """
    ids, reasoning = _normalize_record(record, official=official)
    return ids + reasoning


def repair_file(path, backup_dir, host_closed=False, dry_run=False, *, official=False):
    """Back up and verify closed-file repairs; official adaptation is opt-in.

    The caller must establish that the destination is the official endpoint
    before setting official=True. The default never removes reasoning.content.
    """
    from .history import busy_reason, same_snapshot
    from .history_write_guard import prepare_rewrite
    path = Path(path)
    if path.is_symlink():
        raise OSError("Raw repair source cannot be a symlink")
    def blocked():
        reason = busy_reason(path)
        # Caller must have verified host exit; never ignore an open handle.
        return None if host_closed is True and reason == "recent" else reason
    reason = blocked()
    if reason:
        return {"changed": 0, "skipped": reason}
    original_stat = path.stat()
    original = path.read_bytes()
    lines = []
    count = 0
    id_count = 0
    reasoning_count = 0
    for line in original.splitlines(keepends=True):
        if not line.strip():
            lines.append(line)
            continue
        record = json.loads(line)
        ids, reasoning = _normalize_record(record, official=official)
        changes = ids + reasoning
        id_count += ids
        reasoning_count += reasoning
        count += changes
        if changes:
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + ending
        lines.append(line)
    if not count:
        result = {"changed": 0}
        if official is True:
            result.update(normalized_item_ids=0, reasoning_content_removed=0)
        return result
    if dry_run:
        result = {"changed": 0, "would_change": count}
        if official is True:
            result.update(would_normalize_item_ids=id_count,
                          would_remove_reasoning_content=reasoning_count)
        return result
    updated = b"".join(lines)
    if blocked() or not same_snapshot(path, original_stat, original):
        return {"changed": 0, "skipped": "concurrent-write"}
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / (uuid.uuid4().hex + "-" + path.name)
    with backup.open("xb") as f:
        os.chmod(backup, 0o600)
        f.write(original)
        f.flush()
        os.fsync(f.fileno())
    if backup.read_bytes() != original:
        raise OSError("History backup readback mismatch; original left unchanged")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".message-ids-")
    undo_path = backup.with_suffix(backup.suffix + ".undo.json")
    receipt_written = False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        if blocked() or not same_snapshot(path, original_stat, original):
            return {"changed": 0, "skipped": "concurrent-write"}
        _write_undo(undo_path, {
            "schema_version": 1, "kind": "raw-history-repair",
            "source": str(path.absolute()), "backup": str(backup.absolute()),
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "after_sha256": hashlib.sha256(updated).hexdigest(),
            "after_file_id": _file_id(os.stat(temporary)),
            "original_mode": original_stat.st_mode & 0o777,
        })
        receipt_written = True
        prepare_rewrite(path, host_closed=host_closed)
        if blocked() or not same_snapshot(path, original_stat, original):
            return {"changed": 0, "skipped": "concurrent-write", "undo_receipt": str(undo_path)}
        os.replace(temporary, path)
        _sync_directory(path.parent)
        if path.read_bytes() != updated:
            raise IOError("Message ID repair readback mismatch; original retained in backup")
    except BaseException as error:
        if receipt_written:
            error._history_undo_receipt = str(undo_path)
            try:
                error._history_rollback = rollback_repair(undo_path, host_closed=host_closed)
            except BaseException as rollback_error:
                # Keep the primary failure; the caller can safely report only
                # static codes/types from these private exception objects.
                error._history_rollback_error = rollback_error
        raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    result = {"changed": count, "backup": str(backup), "undo_receipt": str(undo_path)}
    if official is True:
        result.update(normalized_item_ids=id_count,
                      reasoning_content_removed=reasoning_count)
    return result
