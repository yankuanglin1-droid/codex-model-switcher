#!/usr/bin/env python3
"""发布前总自检：把这一路踩过的坑全部变成可复跑的检查。

    python3 tools/preflight.py            # 全部检查（含测试套件）
    python3 tools/preflight.py --fast     # 跳过测试套件

每一条检查都对应一个真实发生过的错误，不是凭空加的规则：

| 检查 | 对应的坑 |
| --- | --- |
| 密钥/个人路径 | 仓库里混进 API Key 或个人目录 |
| git 历史 | 密钥删掉了但还留在提交历史里 |
| 平台专属写法 | Windows 上 import fcntl 直接崩 |
| 旧名残留 | 同一个东西有三套名字，其中一个是错的 |
| 文档链接 | 写了指向不存在文件的链接 |
| App 名一致 | README 让人找的 .app 名字和实际不符 |
| Python 门槛 | 记下的解释器卡 3.11，把系统自带的 3.9 排除了 |
| Windows 编码 | 用 ASCII 写启动器，中文用户名变问号 |
| 脚本语法 | shell 脚本语法错误到运行时才炸 |
| 测试套件 | 功能回归 |
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OK = "  ✅"
BAD = "  ❌"

# 产品名只有这一套，其它写法一律视为旧名残留
APP_NAME = "ChatGPT Model Switcher"
STALE_NAMES = [
    "Codex 多模型切换器",            # 更早的名字
    "Multi-Platform Model Switcher",  # 英文里读作「多操作系统」，实际是「多模型平台」
    "Codex-Multi-Platform",           # 发布资产名里用过
    "Codex-Model-Switcher-macOS.zip",  # 文档里写过、但实际不存在的文件名
]

SKIP_DIRS = {".git", "dist", "__pycache__", ".venv", "node_modules", "build"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".icns", ".ico",
               ".zip", ".gz", ".whl", ".so", ".dylib", ".pyc"}

failures: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    print(("  ✅ " if ok else "  ❌ ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def tracked_text_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        yield path


def check_secrets() -> None:
    print("\n[1] 密钥与个人路径")
    for label, args in (("工作区", []), ("git 历史", ["--history"])):
        result = subprocess.run(
            [sys.executable, "tools/scan_secrets.py"] + args,
            cwd=ROOT, capture_output=True, text=True)
        report(label, result.returncode == 0,
               (result.stdout or result.stderr).strip().splitlines()[0] if result.stdout or result.stderr else "")


def check_portability() -> None:
    print("\n[2] 跨平台写法")
    result = subprocess.run(
        [sys.executable, "tools/check_portability.py"],
        cwd=ROOT, capture_output=True, text=True)
    report("平台专属写法扫描", result.returncode == 0,
           (result.stdout or "").strip().splitlines()[-1] if result.stdout else "")


def check_stale_names() -> None:
    print("\n[3] 产品名一致")
    hits = []
    for path in tracked_text_files():
        if path.as_posix().endswith("tools/preflight.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for stale in STALE_NAMES:
            for m in re.finditer(re.escape(stale), text):
                hits.append("%s:%d 用了旧名「%s」" % (
                    path.relative_to(ROOT).as_posix(), text[:m.start()].count("\n") + 1, stale))
    report("没有旧名残留", not hits, hits[0] if hits else "")


def check_doc_links() -> None:
    print("\n[4] 文档链接")
    missing = []
    for doc in ["README.md", "README.zh-CN.md", "INSTALL-WITH-AI.md", "docs/ai-setup.md"]:
        path = ROOT / doc
        if not path.exists():
            missing.append("%s 不存在" % doc)
            continue
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#]+)\)", text):
            t = target.strip()
            if t.startswith(("http://", "https://", "mailto:", "codex:")):
                continue
            if not (path.parent / t).exists():
                missing.append("%s → %s" % (doc, t))
    report("相对链接都能解析", not missing, missing[0] if missing else "")


def check_app_name() -> None:
    print("\n[5] App 名一致")
    build = (ROOT / "packaging/macos/build_app.sh").read_text(encoding="utf-8")
    m = re.search(r'APP_NAME="\$\{CODEX_SWITCHER_APP_NAME:-([^}]*)\}"', build)
    actual = m.group(1) if m else ""
    report("打包脚本里的 App 名与约定一致", actual == APP_NAME, actual)

    # README 里出现的 .app 路径必须就是这个名字，否则用户按文档找不到文件
    wrong = []
    for doc in ["README.md", "README.zh-CN.md", "docs/troubleshooting.md", "docs/app.md"]:
        path = ROOT / doc
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            for name in re.findall(r'"([^"]+\.app)"', line) + re.findall(r"~/Applications/([^\s\"]+\.app)", line):
                if name[:-4] != APP_NAME:      # 去掉 .app 后缀再比
                    wrong.append("%s:%d 提到 %s" % (doc, line_no, name))
    report("文档里的 .app 路径存在", not wrong, wrong[0] if wrong else "")


def check_python_floor() -> None:
    print("\n[6] Python 版本门槛（行为验证，不看字面）")
    # 这里不检查"代码里有没有写 3.11"—— 注释和提示语里出现 3.11 是正常的，
    # 真正的坑是"用 3.11 当接受门槛"，把系统自带的 3.9 排除掉。
    # 所以直接造一个"只认 3.9、被问 3.11 就拒绝"的假解释器，看它会不会被选中。
    import os
    import stat
    import tempfile

    launch = (ROOT / "packaging/macos/launch.sh").read_text(encoding="utf-8")
    report("launch.sh 用 python_ok 判定已记录的解释器", 'python_ok "$saved"' in launch)
    report("launch.sh 覆盖 python.org 与系统自带路径",
           "/Library/Frameworks/Python.framework" in launch and "/usr/bin/python3" in launch)

    funcs = []
    for pattern in (r"^python_ok\(\) \{.*?\n\}", r"^find_python\(\) \{.*?\n\}"):
        m = re.search(pattern, launch, re.S | re.M)
        if m:
            funcs.append(m.group(0))
    if len(funcs) != 2:
        report("能取出 launch.sh 的解释器发现逻辑", False, "找到 %d 个函数" % len(funcs))
        return

    def probe(version_ok: bool) -> str:
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "python3"
            # 假装是 3.9：问 3.9 通过，问 3.11 拒绝。能不能被选中就是答案。
            fake.write_text(
                "#!/bin/bash\n"
                "case \"$*\" in\n"
                "  *\"3, 9\"*) exit %s ;;\n"
                "  *\"3, 11\"*) exit %s ;;\n"
                "esac\n"
                "exit 1\n" % ("0" if version_ok else "1", "1" if version_ok else "1"))
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            script = "\n".join([
                "STATE_DIR=/nonexistent", "REPO_ROOT=/nonexistent",
                funcs[0], funcs[1],
                'PY=$(find_python) || exit 9',
                'printf "%s" "$PY"',
            ])
            env = dict(os.environ, CODEX_SWITCHER_PYTHON=str(fake))
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                               env=env, timeout=60)
            return r.stdout.strip()

    accepted = probe(True)
    report("3.9 的解释器会被接受（历史 bug：被 3.11 门槛挡掉）",
           accepted.endswith("python3") and accepted != "", accepted or "没找到")
    # 版本太低时，必须跳过这一个候选。注意这里不能要求"什么都找不到"——
    # 脚本会继续往后找系统里真实存在的解释器，那是正确行为。
    rejected = probe(False)
    report("版本太低的解释器会被跳过（不当作合格候选）",
           "tmp" not in rejected, rejected or "没有找到任何合格解释器（也算正确）")


def check_windows_encoding() -> None:
    print("\n[7] Windows 中文路径")
    ps1 = ROOT / "install.ps1"
    if not ps1.exists():
        report("install.ps1 存在", False)
        return
    offenders = []
    for line_no, line in enumerate(ps1.read_text(encoding="utf-8").split("\n"), 1):
        if line.strip().startswith("#"):
            continue          # 注释里提到这个坑是允许的
        if "-Encoding ASCII" in line:
            offenders.append("install.ps1:%d" % line_no)
    report("没有用 ASCII 写启动器", not offenders, "、".join(offenders))

    text = ps1.read_text(encoding="utf-8")
    here = len([l for l in text.split("\n") if l.strip().endswith('@"')])
    close = len([l for l in text.split("\n") if l.strip().startswith('"@')])
    report("PowerShell here-string 配对", here == close and here > 0, "开始 %d / 结束 %d" % (here, close))


def check_shell_syntax() -> None:
    print("\n[8] 脚本语法")
    scripts = sorted(ROOT.rglob("*.sh"))
    bad = []
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append("%s: %s" % (s.relative_to(ROOT), r.stderr.strip().splitlines()[:1]))
    report("%d 个 shell 脚本语法通过" % len(scripts), not bad, str(bad[:1]) if bad else "")


def check_tests() -> None:
    print("\n[10] 测试套件")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    tail = [l for l in (r.stderr or "").strip().split("\n") if l.startswith("Ran ") or l == "OK"]
    report("unittest", r.returncode == 0, " / ".join(tail))


def check_download_links() -> None:
    print("\n[9] 下载链接不会过期")
    # README 里的下载按钮必须指向「固定名」附件（releases/latest/download/<固定名>），
    # 而不是带版本号的文件名 —— 带版本号的链接每次发新版都会失效，
    # 而且手写文件名正是当初「文档写 A、实际发 B」那个坑的来源。
    packer = ROOT / "packaging/macos/package_release.sh"
    if not packer.exists():
        report("打包脚本存在", False)
        return
    text = packer.read_text(encoding="utf-8")
    report("打包脚本会生成固定名副本", "-latest$SUFFIX" in text or "-latest" in text)

    missing = []
    for doc in ["README.md", "README.zh-CN.md"]:
        content = (ROOT / doc).read_text(encoding="utf-8")
        for name in ["Codex-Model-Switcher-macOS-latest-full.zip",
                     "Codex-Model-Switcher-macOS-latest.zip"]:
            if name not in content:
                missing.append("%s 里没有 %s" % (doc, name))
        # 带版本号的下载链接不该出现在文档里
        for hit in re.findall(r"releases/[^\s)\"]*download/[^\s)\"]*v\d+\.\d+\.\d+[^\s)\"]*", content):
            missing.append("%s 用了带版本号的下载链接：%s" % (doc, hit[:60]))
    report("首页下载链接是固定名", not missing, missing[0] if missing else "")


def main() -> int:
    print("=" * 56)
    print("发布前总自检")
    print("=" * 56)
    check_secrets()
    check_portability()
    check_stale_names()
    check_doc_links()
    check_app_name()
    check_python_floor()
    check_windows_encoding()
    check_shell_syntax()
    check_download_links()
    if "--fast" not in sys.argv:
        check_tests()
    print("\n" + "=" * 56)
    if failures:
        print("未通过：%d 项 —— %s" % (len(failures), "、".join(failures)))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
