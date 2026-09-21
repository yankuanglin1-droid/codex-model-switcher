"""Non-destructive message ID normalization for Responses history portability."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path


def normalize_record(record):
    """Only visit typed history items, never user text or arbitrary tool output."""
    if not isinstance(record, dict):
        return 0
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return 0
    items = []
    if record.get("type") == "response_item":
        items.append(payload)
    elif record.get("type") == "compacted":
        for field in ("replacement_history", "guardian_history"):
            value = payload.get(field)
            if isinstance(value, list):
                items.extend(value)
    changed = 0
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        value = item.get("id")
        if isinstance(value, str) and value and not value.startswith("msg"):
            item["id"] = "msg_" + hashlib.sha256(value.encode()).hexdigest()[:32]
            changed += 1
    return changed


def repair_file(path, backup_dir, host_closed=False):
    """Refuse open/recent files, preserve all records, back up and verify writes."""
    from .history import busy_reason
    path = Path(path)
    def blocked():
        reason = busy_reason(path)
        # Caller must have verified host exit; never ignore an open handle.
        return None if host_closed and reason == "recent" else reason
    reason = blocked()
    if reason:
        return {"changed": 0, "skipped": reason}
    original = path.read_bytes()
    lines = []
    count = 0
    for line in original.splitlines(keepends=True):
        if not line.strip():
            lines.append(line)
            continue
        record = json.loads(line)
        changes = normalize_record(record)
        count += changes
        if changes:
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + ending
        lines.append(line)
    if not count:
        return {"changed": 0}
    updated = b"".join(lines)
    if blocked() or path.read_bytes() != original:
        return {"changed": 0, "skipped": "concurrent-write"}
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / (uuid.uuid4().hex + "-" + path.name)
    with backup.open("xb") as f:
        os.chmod(backup, 0o600)
        f.write(original)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".message-ids-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        if blocked() or path.read_bytes() != original:
            return {"changed": 0, "skipped": "concurrent-write"}
        os.replace(temporary, path)
        if path.read_bytes() != updated:
            raise IOError("Message ID repair readback mismatch; original retained in backup")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"changed": count, "backup": str(backup)}
