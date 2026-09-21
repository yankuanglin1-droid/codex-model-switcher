"""Read-only scheduler diagnostics; never rewrite schedule files or prompts."""
from . import configfile, paths, threads


def inspect(current_provider, current_model):
    result = {"total": 0, "fixed_model": 0, "heartbeat": 0,
              "needs_review": 0, "unreadable": 0, "trigger_verified": False}
    files = list((paths.codex_home() / "automations").glob("*/automation.toml"))
    bindings = None
    for path in files:
        result["total"] += 1
        try:
            data = configfile.parse(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, configfile.ConfigError):
            result["unreadable"] += 1
            continue
        if data.get("kind") == "heartbeat":
            result["heartbeat"] += 1
            if bindings is None:
                bindings = {item["id"]: item for item in threads.list_threads()}
            target = data.get("target_thread_id") or data.get("targetThreadId")
            bound = bindings.get(target)
            if not bound or bound.get("provider") != current_provider:
                result["needs_review"] += 1
        elif data.get("model"):
            result["fixed_model"] += 1
            if (data["model"] != current_model
                    or data.get("model_provider", current_provider) != current_provider):
                result["needs_review"] += 1
    return result
