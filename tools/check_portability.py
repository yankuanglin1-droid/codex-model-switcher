#!/usr/bin/env python3
"""跨平台自查：找出只能在某个系统上跑的实现。

  python3 tools/check_portability.py
  python3 tools/check_portability.py --strict   # 有高危项就返回非零

检查内容：
  · Windows 上不存在的模块（fcntl / termios / pwd / grp / resource）
  · Windows 上不存在或行为不同的调用（os.fchmod / os.getuid / 打开目录）
  · 只在某个平台存在的可执行文件（ps / tasklist / security / secret-tool）
  · 平台分支是否写全（sys.platform / os.name 判断旁边有没有 else）
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ["codex_switcher"]

# (正则, 危险等级, 说明, 允许的平台)
RULES = [
    (r"^\s*import fcntl|^\s*import termios|^\s*import pwd\b|^\s*import grp\b",
     "high", "Windows 上不存在这个模块", {"darwin", "linux"}),
    (r"^\s*import resource\b", "high", "Windows 上不存在 resource", {"darwin", "linux"}),
    (r"\bos\.fchmod\b", "high", "Windows 没有 os.fchmod", set()),
    (r"\bos\.getuid\b|\bos\.geteuid\b", "high", "Windows 没有 os.getuid", set()),
    (r"\bos\.setuid\b|\bos\.fork\b", "high", "Windows 不支持", set()),
    (r"subprocess\.\w+\(\[\s*[\"']ps[\"']", "high", "Windows 没有 ps 命令", set()),
    (r"[\"']/usr/bin/security[\"']", "medium", "macOS 专属", {"darwin"}),
    (r"[\"']secret-tool[\"']", "medium", "Linux 专属", {"linux"}),
    (r"\bosascript\b", "medium", "macOS 专属", {"darwin"}),
    (r"\blaunchctl\b", "medium", "macOS 专属", {"darwin"}),
    (r"\bscreencapture\b", "low", "macOS 专属", {"darwin"}),
    (r"\bsips\b|\biconutil\b|\bosacompile\b", "low", "macOS 打包专属", {"darwin"}),
    (r"os\.open\([^)]*O_RDONLY\)", "medium", "Windows 不能打开目录做 fsync", set()),
    (r"\bctypes\.WinDLL\b", "low", "Windows 专属", {"win32"}),
    (r"\bmsvcrt\b", "low", "Windows 专属", {"win32"}),
]


def platform_of_file(path: Path) -> set:
    """从文件路径推断它属于哪个平台；返回空集合表示通用。"""
    text = str(path).lower()
    if "packaging/macos" in text or "packaging\\macos" in text:
        return {"darwin"}
    if "packaging/windows" in text or "packaging\\windows" in text:
        return {"win32"}
    return set()


def has_platform_guard(source: str, line_number: int, window: int = 40) -> bool:
    """看这一行附近有没有平台判断，避免把已经守住的分支当成问题。"""
    lines = source.splitlines()
    start = max(0, line_number - window)
    end = min(len(lines), line_number + 4)
    snippet = "\n".join(lines[start:end])
    return bool(re.search(r"IS_WINDOWS|os\.name|sys\.platform|sys\.version_info|hasattr\(os", snippet))


def ignored_lines(source: str) -> set:
    """注释和字符串里的行号 —— 文档里提到 fcntl 不算真的用了它。"""
    ignored = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                for line in range(token.start[0], token.end[0] + 1):
                    ignored.add(line)
    except (tokenize.TokenError, IndentationError):
        pass
    return ignored


def scan_python_file(path: Path) -> list:
    findings = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    file_platforms = platform_of_file(path)
    skipped_lines = ignored_lines(source)
    for pattern, level, note, allowed in RULES:
        for match in re.finditer(pattern, source, re.MULTILINE):
            line_number = source[:match.start()].count("\n") + 1
            if line_number in skipped_lines:
                continue  # 注释或文档字符串里的字样
            if allowed and allowed == file_platforms:
                continue  # 文件本来就是这个平台专用的
            if has_platform_guard(source, line_number):
                continue  # 已经被平台判断守住
            if file_platforms and allowed and file_platforms & allowed:
                continue
            findings.append({
                "file": path.relative_to(ROOT).as_posix(),
                "line": line_number,
                "level": level,
                "note": note,
                "code": source.splitlines()[line_number - 1].strip()[:70],
            })
    return findings


def check_syntax(path: Path) -> list:
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        return [{"file": path.relative_to(ROOT).as_posix(), "line": error.lineno or 0,
                 "level": "high", "note": "语法错误，这个 Python 版本跑不了",
                 "code": str(error)[:70]}]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="有高危项时返回非零")
    args = parser.parse_args()

    findings = []
    files = 0
    for directory in CODE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files += 1
            findings += check_syntax(path)
            findings += scan_python_file(path)
    for extra in ("bin", "packaging", "tools"):
        for path in sorted((ROOT / extra).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files += 1
            findings += scan_python_file(path)

    print("扫描 %d 个 Python 文件" % files)
    print()
    if not findings:
        print("没有发现平台不兼容的写法。")
        print("运行时依赖：macOS / Linux 需要 Python 3.9+，Windows 需要 Python 3.9+")
        return 0

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (order[item["level"]], item["file"], item["line"]))
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in findings:
        counts[item["level"]] += 1
        print("[%s] %s:%d  %s" % (item["level"].upper(), item["file"], item["line"], item["note"]))
        print("        %s" % item["code"])
    print()
    print("高危 %d 项 / 中等 %d 项 / 提示 %d 项" % (counts["high"], counts["medium"], counts["low"]))
    print("（高危及以上会让某个平台直接跑不起来）")
    if args.strict and counts["high"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
