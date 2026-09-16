"""检查是不是最新版。

只做只读的一次 HTTP 查询，结果缓存 24 小时，避免每次打开界面都去打扰 GitHub。
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.request
from typing import Dict, Optional

from . import PROJECT_URL, __version__, paths

RELEASES_API = PROJECT_URL.replace("https://github.com/", "https://api.github.com/repos/") + "/releases/latest"
CACHE_HOURS = 24


def version_tuple(text: str):
    numbers = re.findall(r"\d+", (text or "").lstrip("vV"))
    return tuple(int(item) for item in numbers[:3]) or (0,)


def _cache_path():
    return paths.state_dir() / "update.json"


def read_cache() -> Optional[Dict]:
    try:
        return json.loads(_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _fresh(cache: Dict) -> bool:
    try:
        checked = datetime.datetime.fromisoformat(cache["checked_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age = datetime.datetime.now() - checked
    return age.total_seconds() < CACHE_HOURS * 3600


def latest_release(timeout: int = 12) -> Dict:
    """向 GitHub 问一次最新 Release。失败不抛错，返回 status=failed。"""
    request = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-model-switcher/" + __version__,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            document = json.loads(response.read(200000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "no-release"}
        return {"status": "failed", "http": exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "detail": type(exc).__name__}
    return {
        "status": "ok",
        "tag": document.get("tag_name") or "",
        "url": document.get("html_url") or PROJECT_URL + "/releases",
        "published_at": document.get("published_at") or "",
    }


def check(force: bool = False, timeout: int = 12) -> Dict:
    """返回 {status, current, latest, up_to_date, url, checked_at}。"""
    cache = read_cache()
    if not force and cache and _fresh(cache):
        result = dict(cache)
    else:
        info = latest_release(timeout=timeout)
        if info.get("status") == "ok":
            result = {
                "status": "ok",
                "current": __version__,
                "latest": info["tag"],
                "up_to_date": version_tuple(info["tag"]) <= version_tuple(__version__),
                "url": info["url"],
            }
        elif info.get("status") == "no-release":
            result = {"status": "no-release", "current": __version__, "url": PROJECT_URL + "/releases"}
        else:
            result = {"status": "failed", "current": __version__,
                      "url": PROJECT_URL + "/releases", "detail": info.get("detail") or info.get("http")}
        result["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            paths.ensure_dir(paths.state_dir())
            _cache_path().write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        except OSError:
            pass
    result.setdefault("current", __version__)
    return result


def describe(result: Dict) -> str:
    if result.get("status") != "ok":
        return "无法确认是否有新版本（网络不可用或仓库还没有 Release）"
    if result.get("up_to_date"):
        return "已是最新版本 v%s" % result.get("current")
    return "有新版本 %s（当前 v%s）：%s" % (
        result.get("latest"), result.get("current"), result.get("url"))
