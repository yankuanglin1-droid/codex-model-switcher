"""Keep the host's durable history cursor consistent with explicit file repairs.

Never clear displayed items: only the replay cursor is disposable. Invalidating
before replacing the file is crash-safe: an interrupted repair can replay the
unchanged original, whereas replacing first can leave a cursor beyond EOF.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import paths


def _require_host_closed(host_closed):
    from . import codexapp
    # Per-file lsof alone is insufficient: a live host can cache the cursor even
    # when it is not currently holding this rollout open.
    if sys.platform == "darwin":
        try:
            closed = codexapp.is_running() is False
        except Exception as error:
            raise OSError("Cannot verify ChatGPT/Codex shutdown; original left unchanged") from error
        if not closed:
            raise OSError("Close ChatGPT/Codex before repairing indexed history")
        codexapp.assert_history_idle(paths.codex_home())
    elif host_closed is not True:
        raise OSError("Indexed history repair requires verified host shutdown")


def prepare_rewrite(path, *, host_closed=False):
    home = paths.codex_home()
    path = Path(path)
    # Synthetic/offline exports are not indexed by this Codex home.  They do
    # not have a host cursor to invalidate; production rollouts remain under
    # CODEX_HOME and take the strict path below.
    try:
        path.resolve().relative_to(home.resolve())
    except ValueError:
        return {"invalidated": 0}
    indexed = any(home.glob("thread_history_*.sqlite"))
    if not indexed and not any(home.glob("state_*.sqlite")):
        return {"invalidated": 0}
    if indexed:
        _require_host_closed(host_closed)
    try:
        with path.open("rb") as stream:
            header = json.loads(next(line for line in stream if line.strip()))
        payload = header.get("payload") if isinstance(header, dict) else None
        thread_id = payload.get("id") if isinstance(payload, dict) else None
    except (ValueError, StopIteration) as error:
        raise OSError("Cannot identify indexed history; original left unchanged") from error
    if not isinstance(header, dict) or header.get("type") != "session_meta" or not isinstance(thread_id, str) or not thread_id:
        raise OSError("Cannot identify indexed history; original left unchanged")
    from . import projection
    report = projection.invalidate_projection(
        home, [thread_id], host_closed=True, force=True,
        rollout_paths={thread_id: path})
    # A partial or unverifiable invalidation must not authorize the file commit.
    if not isinstance(report, dict) or report.get("error") or report.get("errors"):
        raise OSError("History projection invalidation failed; original left unchanged")
    count = report.get("invalidated")
    receipts, skipped = report.get("receipts"), report.get("skipped")
    if (type(count) is not int or count < 0 or not isinstance(receipts, list)
            or len(receipts) != count or not isinstance(skipped, list)
            or (count and not report.get("backup")) or not (receipts or skipped)):
        raise OSError("Cannot verify history projection invalidation; original left unchanged")
    for entry in receipts + skipped:
        if (not isinstance(entry, dict) or entry.get("thread_id") != thread_id
                or not isinstance(entry.get("rollout_path"), (str, Path))
                or Path(entry["rollout_path"]).resolve() != path.resolve()):
            raise OSError("History projection receipt does not match source; original left unchanged")
    if any(entry.get("reason") not in {"missing_cursor", "already_reset"} for entry in skipped):
        raise OSError("History projection was not invalidated; original left unchanged")
    if indexed:
        _require_host_closed(host_closed)
    return report
