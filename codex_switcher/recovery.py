"""App-owned, asynchronous repair between graceful host exit and relaunch."""
from __future__ import annotations

import json
import sys
import threading
import time
from . import codexapp, history, message_ids, paths

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE = {"phase": "idle", "checked": 0, "changed": 0, "skipped": 0, "failed": 0}


def status():
    with _STATE_LOCK:
        return dict(_STATE)


def _set(**values):
    with _STATE_LOCK:
        _STATE.update(values)


def start():
    if sys.platform != "darwin":
        return {"error": "Automatic repair and restart currently requires macOS."}
    if not _LOCK.acquire(blocking=False):
        return status()
    _set(phase="quitting", checked=0, changed=0, skipped=0, failed=0,
         error=None, reopened=False)
    threading.Thread(target=_run, daemon=True, name="history-recovery").start()
    return status()


def _run():
    closed = False
    try:
        if not codexapp.find_app():
            raise RuntimeError("ChatGPT/Codex application was not found")
        if codexapp.is_running() and not codexapp._quit():
            raise RuntimeError("Application did not exit; repair cancelled")
        if codexapp.is_running():
            raise RuntimeError("Application is still running; repair cancelled")
        closed = True
        _set(phase="repairing")
        backup = paths.state_dir() / "history-backups" / ("app-repair-%s" % time.time_ns())
        files = history.recent_rollouts(0)
        _set(total=len(files))
        for path in files:
            if codexapp.is_running():
                raise RuntimeError("Application reopened during repair; stopped to protect history")
            # Repair only histories bound to the official provider. Other
            # providers' histories are handled when they are explicitly moved.
            try:
                with path.open(encoding="utf-8") as f:
                    head = json.loads(f.readline())
                if head.get("type") != "session_meta":
                    _set(skipped=status()["skipped"] + 1)
                    continue
                if head.get("payload", {}).get("model_provider", "openai") != "openai":
                    continue
                result = message_ids.repair_file(path, backup, host_closed=True)
                state = status()
                _set(checked=state["checked"] + 1,
                     changed=state["changed"] + result.get("changed", 0),
                     skipped=state["skipped"] + int(bool(result.get("skipped"))))
            except (OSError, ValueError):
                _set(failed=status()["failed"] + 1)
        _set(phase="reopening")
    except Exception as exc:
        # Never expose provider response bodies, credentials or history text.
        _set(phase="error", error=type(exc).__name__)
    finally:
        if closed:
            try:
                _set(reopened=codexapp._reopen())
            except Exception:
                _set(reopened=False)
        if status()["phase"] == "reopening":
            _set(phase="done")
        _LOCK.release()
