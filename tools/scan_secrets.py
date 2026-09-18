#!/usr/bin/env python3
"""发布前自检：确认仓库里没有密钥、没有个人路径。

  python3 tools/scan_secrets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = [
    ("疑似 API Key", re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}")),
    ("疑似 MiniMax Key", re.compile(r"\bsk-cp-[A-Za-z0-9._\-]{16,}")),
    ("疑似 Anthropic Key", re.compile(r"\bsk-ant-[A-Za-z0-9._\-]{16,}")),
    ("疑似 OpenRouter Key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}")),
    ("疑似 智谱 Key", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b")),
    ("疑似 GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("疑似 OpenAI 组织密钥", re.compile(r"\bsess-[A-Za-z0-9]{20,}")),
    ("个人主目录路径", re.compile(r"/Users/[A-Za-z0-9._\-]+/")),
    ("个人主目录路径（Linux）", re.compile(r"/home/[A-Za-z0-9._\-]+/")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

ALLOWED = {
    "tools/scan_secrets.py",  # 本文件本身包含用于检测的正则
}

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "dist", "build"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".icns", ".ico",
               ".zip", ".gz", ".whl", ".so", ".dylib", ".pyc"}


def scan_text(text: str, where: str, findings: list, allowed: set) -> None:
    if where in allowed:
        return
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            line = text[:match.start()].count("\n") + 1
            # 只打印前 12 个字符做定位，绝不把完整密钥打到终端或日志里
            findings.append((where, line, label, match.group(0)[:12] + "…"))


def _versioned_files():
    """git 视角下"会被纳入版本控制"的相对路径集合；拿不到就返回 None。

    工作区扫描要回答的是「有没有密钥会进仓库」，所以**已经被 .gitignore 忽略的
    本地文件不该算** —— 例如用户自己放在 private-install/ 里的密钥文件，
    它确实在本机明文躺着，但不会跟着发布出去（git 历史和远端那两项另行校验）。
    拿不到 git 信息时返回 None，调用方退回全量扫描：宁可多报，不可漏报。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return {name.decode("utf-8", "replace")
            for name in result.stdout.split(b"\0") if name}


def scan() -> int:
    findings = []
    versioned = _versioned_files()
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED:
            continue
        if versioned is not None and relative not in versioned:
            continue  # 已被 .gitignore 忽略，不会进仓库
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scan_text(text, relative, findings, ALLOWED)

    if not findings:
        print("通过（工作区）：没有发现密钥或个人路径。")
        return 0
    print("工作区发现 %d 处需要处理的内容：" % len(findings))
    for relative, line, label, sample in findings:
        print("  %s:%d  %s  %s" % (relative, line, label, sample))
    return 1


def scan_history() -> int:
    """扫全部 git 历史。

    只在工作区扫是不够的：密钥就算后来删掉，也仍然留在提交历史里，
    任何人都能 `git log -p` 翻出来。凡是推到公开仓库过的东西，删掉≠消失。
    """
    import subprocess

    try:
        commits = subprocess.run(
            ["git", "rev-list", "--all"], cwd=ROOT,
            capture_output=True, text=True, timeout=120, check=True).stdout.split()
    except (subprocess.SubprocessError, OSError) as exc:
        print("读不到 git 历史：%s" % exc, file=sys.stderr)
        return 2

    findings = []
    seen_blobs = set()
    for commit in commits:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-z", commit], cwd=ROOT,
            capture_output=True, timeout=180)
        if result.returncode != 0:
            continue
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, name = entry.partition(b"\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != b"blob":
                continue
            blob = parts[2].decode()
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            path = name.decode("utf-8", "replace")
            if path in ALLOWED or Path(path).suffix.lower() in SKIP_SUFFIX:
                continue
            content = subprocess.run(
                ["git", "cat-file", "-p", blob], cwd=ROOT,
                capture_output=True, timeout=60).stdout
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            scan_text(text, path, findings, ALLOWED)

    if not findings:
        print("通过（git 历史 %d 个提交 / %d 个文件版本）：没有发现密钥或个人路径。"
              % (len(commits), len(seen_blobs)))
        return 0
    print("git 历史里发现 %d 处需要处理的内容：" % len(findings))
    for path, line, label, sample in findings:
        print("  %s:%d  %s  %s" % (path, line, label, sample))
    return 1


if __name__ == "__main__":
    if "--history" in sys.argv:
        raise SystemExit(scan_history())
    raise SystemExit(scan())
