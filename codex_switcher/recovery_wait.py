"""Read-only completion gate for a one-off, explicitly authorized recovery job.

The caller supplies independently verified running turn IDs. Existing contents
are not treated as live activity; only new lifecycle records are observed. No
host process is stopped and no history is changed by this module.
"""
from __future__ import annotations

import json
from pathlib import Path
import time


class CompletionGate:
    def __init__(self, sources, required, running=(), *, quiet_seconds=15, clock=time.monotonic,
                 allow_followup_turns=False):
        self.clock = clock
        self.required = tuple(required)
        self.pending = {tuple(pair) for pair in running} | {self.required}
        self.finished = False
        self.allow_followup_turns = allow_followup_turns is True
        self.quiet_seconds = quiet_seconds
        self.changed_at = clock()
        self.files = {}
        for tid, path in sources.items():
            self._add(tid, Path(path), existing=True)

    def _add(self, tid, path, *, existing=False):
        if not path.is_file():
            if tid == self.required[0]:
                raise OSError('Required task history is missing')
            return
        info = path.stat()
        offset = info.st_size if existing else 0
        # If the writer is partway through a JSON line, read that whole line
        # after it becomes complete, rather than silently ignoring its event.
        if offset:
            with path.open('rb') as stream:
                stream.seek(offset - 1)
                if stream.read(1) != b'\n':
                    while offset:
                        start = max(0, offset - 65536)
                        stream.seek(start)
                        block = stream.read(offset - start)
                        found = block.rfind(b'\n')
                        if found >= 0:
                            offset = start + found + 1
                            break
                        offset = start
        self.files[tid] = dict(path=path, device=info.st_dev, inode=info.st_ino,
                               offset=offset, observed_size=info.st_size if existing else -1,
                               mtime=info.st_mtime_ns if existing else -1)

    def poll(self, sources):
        if self.required[0] not in sources:
            raise OSError('Required task is no longer in active scope')
        for tid, name in sources.items():
            path = Path(name)
            if tid not in self.files:
                self._add(tid, path)
                self.changed_at = self.clock()
            row = self.files.get(tid)
            if row is None:
                continue
            if path != row['path']:
                raise OSError('Task history path changed while waiting')
            info = path.stat()
            if (info.st_dev, info.st_ino) != (row['device'], row['inode']) or info.st_size < row['observed_size']:
                raise OSError('Task history was replaced or truncated while waiting')
            if (info.st_size, info.st_mtime_ns) == (row['observed_size'], row['mtime']):
                continue
            if info.st_size == row['observed_size']:
                raise OSError('Task history changed without an append')
            self.changed_at = self.clock()
            with path.open('rb') as stream:
                stream.seek(row['offset'])
                while stream.tell() < info.st_size:
                    line = stream.readline()
                    if not line.endswith(b'\n'):
                        break
                    row['offset'] = stream.tell()
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get('type') != 'event_msg':
                        continue
                    event = record.get('payload', {})
                    kind, turn = event.get('type'), event.get('turn_id')
                    if kind not in ('task_started', 'task_complete', 'turn_aborted'):
                        continue
                    if not isinstance(turn, str) or not turn:
                        raise OSError('Task lifecycle identity is ambiguous')
                    pair = (tid, turn)
                    if kind == 'task_started':
                        if (tid == self.required[0] and pair != self.required
                                and not self.allow_followup_turns):
                            raise OSError('A new user turn started; deferred recovery cancelled')
                        self.pending.add(pair)
                    else:
                        self.pending.discard(pair)
                        if pair == self.required:
                            if kind == 'turn_aborted':
                                raise OSError('Required turn was interrupted; recovery cancelled')
                            self.finished = True
            row.update(observed_size=info.st_size, mtime=info.st_mtime_ns)
        if any(tid not in sources for tid, _ in self.pending):
            raise OSError('A running task left the authorized active scope')
        return self.finished and not self.pending and self.clock() - self.changed_at >= self.quiet_seconds
