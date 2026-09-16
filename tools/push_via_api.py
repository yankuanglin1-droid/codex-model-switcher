#!/usr/bin/env python3
"""当 github.com:443 连不上时，改用 GitHub API 推送。

有些网络环境下 api.github.com 能通、github.com 不通，`git push` 会一直失败。
这个脚本走 Git Data API（create blob → tree → commit → update ref）把当前
HEAD 推上去，效果和 git push 一样。

用法：
  python3 tools/push_via_api.py                 # 推当前分支
  python3 tools/push_via_api.py --dry-run       # 只说明会改什么

前提：本机已用 `gh auth login` 登录，且本地领先远程（远程是本地 HEAD 的祖先）。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, **kwargs)


def gh(method: str, path: str, payload=None):
    command = ["gh", "api", "--method", method, path]
    if payload is None:
        result = run(command)
    else:
        result = run(command + ["--input", "-"], input=json.dumps(payload))
    if result.returncode != 0:
        raise RuntimeError("gh api %s %s 失败：%s" % (method, path, (result.stderr or "").strip()[:300]))
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def local_files():
    """返回 [(path, mode, blob_sha)]，来自 git 索引。"""
    result = run(["git", "ls-files", "-s"])
    entries = []
    for line in result.stdout.splitlines():
        meta, path = line.split("\t", 1)
        mode, sha, _ = meta.split()
        entries.append((path, mode, sha))
    return entries


def remote_tree(repo: str, branch: str):
    info = gh("GET", "repos/%s/git/ref/heads/%s" % (repo, branch))
    head_sha = info["object"]["sha"]
    tree = gh("GET", "repos/%s/git/trees/%s?recursive=1" % (repo, head_sha))
    mapping = {}
    for item in tree.get("tree", []):
        if item["type"] == "blob":
            mapping[item["path"]] = (item["sha"], item.get("mode", "100644"))
    return head_sha, tree["sha"], mapping


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=None, help="owner/name，默认从 origin 推断")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default=None, help="提交信息，默认用本地 HEAD 的")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = args.repo
    if not repo:
        origin = run(["git", "remote", "get-url", "origin"]).stdout.strip()
        repo = origin.rstrip(".git").split("github.com/")[-1]
    message = args.message or run(["git", "log", "-1", "--pretty=%B"]).stdout.strip()

    head_sha, base_tree_sha, remote = remote_tree(repo, args.branch)
    files = local_files()
    print("远程 HEAD：%s" % head_sha[:12])
    print("本地文件：%d 个" % len(files))

    entries = []
    uploaded = 0
    for path, mode, sha in files:
        if remote.get(path, (None, None))[0] == sha:
            continue  # 内容一致，复用远程 blob
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})

    removed = [path for path in remote if path not in {p for p, _, _ in files}]
    for path in removed:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    if not entries:
        print("远程已经和本地一致，不需要推送。")
        return 0

    print("需要上传 %d 个改动文件，删除 %d 个。" % (len(entries) - len(removed), len(removed)))
    for item in entries:
        mark = "删除" if item["sha"] is None else "更新"
        print("  %s %s" % (mark, item["path"]))
    if args.dry_run:
        print("\n（dry-run，没有实际推送）")
        return 0

    # 逐个上传 blob（GitHub 会按内容去重，同内容的 sha 不变）
    for item in entries:
        if item["sha"] is None:
            continue
        blob_path = item["path"]
        payload = gh("POST", "repos/%s/git/blobs" % repo, {
            "content": base64.b64encode((REPO_ROOT / blob_path).read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
        item["sha"] = payload["sha"]
        uploaded += 1
    print("已上传 %d 个文件内容。" % uploaded)

    tree = gh("POST", "repos/%s/git/trees" % repo, {"base_tree": base_tree_sha, "tree": entries})
    commit = gh("POST", "repos/%s/git/commits" % repo, {
        "message": message,
        "tree": tree["sha"],
        "parents": [head_sha],
    })
    gh("PATCH", "repos/%s/git/refs/heads/%s" % (repo, args.branch), {
        "sha": commit["sha"],
        "force": False,
    })
    print("推送完成：%s" % commit["sha"][:12])
    print("https://github.com/%s/commit/%s" % (repo, commit["sha"]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
