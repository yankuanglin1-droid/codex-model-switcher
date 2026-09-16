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


def scan() -> int:
    findings = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                findings.append((relative, line, label, match.group(0)[:12] + "…"))

    if not findings:
        print("通过：没有发现密钥或个人路径。")
        return 0
    print("发现 %d 处需要处理的内容：" % len(findings))
    for relative, line, label, sample in findings:
        print("  %s:%d  %s  %s" % (relative, line, label, sample))
    return 1


if __name__ == "__main__":
    raise SystemExit(scan())
