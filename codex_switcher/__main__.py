import sys

from .cli import main


def _make_output_never_crash() -> None:
    """让输出永远不会因为编码问题把程序搞崩。

    中文 Windows 的控制台代码页是 936，编码表里没有 ✅ ❌ ✗ ⚠ 这些符号。
    一旦输出被重定向到文件或管道（`codex-switcher export > x.json`、
    日志重定向等），Python 就会用 cp936 编码，打印这些符号会直接抛
    UnicodeEncodeError，把本来正常的命令打断。
    这里把错误策略改成 replace：编不出来的字符显示成 ?，程序照常跑完。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass          # 老 Python / 被替换过的流：忽略，不影响主流程


if __name__ == "__main__":
    _make_output_never_crash()
    raise SystemExit(main())
