#!/usr/bin/env python3
"""生成 docs/screenshot.png（界面截图）。

在一个临时 CODEX_HOME 里造几个演示平台，用无头 Chrome 截一张图。
**不会写入真实的钥匙串**：脚本会把凭据后端强制切成文件回退，且只写演示用的假密钥。

  python3 tools/make_screenshot.py           # 静态图 docs/screenshot.png
  python3 tools/make_screenshot.py --gif     # 另外生成 docs/demo.gif（动态演示）
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_PROVIDERS = [
    ("deepseek", "DeepSeek", "https://api.deepseek.com", "native",
     ["deepseek-flash", "deepseek-v4-pro"], "DEMO-KEY-DEEPSEEK-0001"),
    ("minimax", "MiniMax", "https://api.minimax.cn/v1", "native",
     ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5",
      "MiniMax-M2.5-highspeed", "MiniMax-M2.1", "MiniMax-M2"], "DEMO-KEY-MINIMAX-0002"),
    ("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "bridge",
     ["kimi-k2-0905-preview", "kimi-k2-turbo-preview", "moonshot-v1-128k"],
     "DEMO-KEY-MOONSHOT-0003"),
    ("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/v1", "native",
     ["glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo",
      "glm-4.7", "glm-4.6"], "DEMO-KEY-ZHIPU-0004"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# 兜底截图器：Chrome 的 GPU/沙箱在某些机器上起不来（报
# "GPU process isn't usable" / "sandbox initialization failed"），
# agent-browser（Playwright 系）不受影响，有就用它顶上。
AGENT_BROWSER_CANDIDATES = [
    "/opt/homebrew/bin/agent-browser",
    "/usr/local/bin/agent-browser",
]

# 动态演示依次展示这几个状态：换平台 → 换模型 → 能力查看 → 切回官方。
# 每一帧都是真实界面截图，只是把切换过程连起来。
# 第三个元素是附加视图："caps" 表示拍「能力查看」页（只看选中的那个模型）。
GIF_STATES = [
    ("deepseek", "deepseek-flash", ""),
    ("minimax", "MiniMax-M3", ""),
    ("minimax", "MiniMax-M3", "caps"),
    ("zhipu", "glm-5.3", ""),
    ("openai", "gpt-5-codex", ""),
]


_CHROME_BROKEN = False


def shoot(chrome: str, url: str, target: Path, timeout: int = 60) -> bool:
    """截一张图落到 target。成功返回 True。

    注意：先写到临时文件、成功后再替换过去。直接对 target 截图的话，
    「文件存在」判断会被上一次运行的旧图骗过去 —— Chrome 明明失败了，
    脚本却报成功，README 上就一直挂着旧界面。
    """
    global _CHROME_BROKEN
    staging = target.with_suffix(".shooting%s" % target.suffix)
    staging.unlink(missing_ok=True)
    # Chrome 在一部分机器上起不来（GPU/沙箱），失败一次就别再浪费时间等它超时，
    # 后面全部改走 agent-browser
    if not _CHROME_BROKEN:
        try:
            result = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 "--force-device-scale-factor=2", "--window-size=1360,860",
                 "--virtual-time-budget=4000",
                 "--screenshot=%s" % staging, url],
                capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _CHROME_BROKEN = True
            sys.stderr.write("Chrome 超时（%ds），改用 agent-browser。\n" % timeout)
        else:
            if staging.exists():
                os.replace(staging, target)
                return True
            _CHROME_BROKEN = True
            sys.stderr.write("Chrome 截图失败 (%s)：%s\n" % (
                url, (result.stderr or b"").decode("utf-8", "replace")[-300:].strip()))
    fallback = find_agent_browser()
    if fallback and shoot_agent_browser(fallback, url, staging):
        os.replace(staging, target)
        return True
    return False


def build_gif(frames: list, target: Path) -> bool:
    """把若干张截图连成一个带交叉淡入的 GIF。

    用 ffmpeg 的 xfade：每张停留 HOLD 秒，相邻两张之间淡入淡出 FADE 秒。
    先缩到 1100px 宽再生成调色板，否则 2720px 的原图会让 GIF 大到没法用。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or len(frames) < 2:
        return False

    HOLD, FADE, WIDTH = 2.2, 0.5, 1100
    inputs = []
    for frame in frames:
        inputs += ["-loop", "1", "-t", "%.2f" % HOLD, "-i", str(frame)]

    # 依次拼接：第 k 次拼接的 offset = 当前总时长 - FADE
    parts, length, last = [], HOLD, "0:v"
    for index in range(1, len(frames)):
        offset = length - FADE
        label = "x%d" % index
        parts.append("[%s][%d:v]xfade=transition=fade:duration=%.2f:offset=%.2f[%s]"
                     % (last, index, FADE, offset, label))
        length = length + HOLD - FADE
        last = label
    chain = ("%s;[%s]scale=%d:-1:flags=lanczos,split[a][b];"
             "[a]palettegen=max_colors=200[p];[b][p]paletteuse=dither=bayer"
             % (";".join(parts), last, WIDTH))

    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error"] + inputs +
        ["-filter_complex", chain, "-loop", "0", str(target)],
        capture_output=True, timeout=300)
    return result.returncode == 0 and target.exists()


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def find_agent_browser() -> str:
    for candidate in AGENT_BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("agent-browser") or ""


def shoot_agent_browser(browser: str, url: str, target: Path) -> bool:
    """用 agent-browser 截一张 1360x860@2x 的图。"""
    commands = [
        [browser, "open", url],
        [browser, "set", "viewport", "1360", "860", "2"],
        [browser, "screenshot", str(target)],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            return False
        if result.returncode != 0:
            sys.stderr.write("agent-browser %s 失败：%s\n" % (
                command[1], (result.stderr or b"").decode("utf-8", "replace")[-300:].strip()))
            return False
    # 页面要拉取状态接口，等它把数据填进来再截
    time.sleep(2.0)
    subprocess.run([browser, "screenshot", str(target)],
                   capture_output=True, timeout=90)
    return target.exists()

def main() -> int:
    chrome = find_chrome()
    if not chrome:
        print("没有找到 Chrome / Chromium，跳过截图。")
        return 0

    mock_port = start_mock_balance_server()
    workdir = tempfile.mkdtemp(prefix="codex-switcher-shot-")
    os.environ["CODEX_HOME"] = workdir
    Path(workdir, "config.toml").write_text(
        'model_provider = "deepseek"\nmodel = "deepseek-flash"\n\n[desktop]\nappearanceTheme = "dark"\n')

    from codex_switcher import engine, secrets, state as state_module

    # 演示环境：强制文件回退，绝不碰真实钥匙串
    secrets._macos_available = lambda: False
    # 截图上展示 macOS 用户真实会看到的字样
    secrets.backend_label = lambda: "macOS 钥匙串"
    # 界面按这个代号查词典翻译，所以也要一起改，否则英文截图会显示成
    # "local file (0600, weakest)" —— 与实际不符
    secrets.backend = lambda: "keychain"
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    state = state_module.load()
    for provider_id, label, base_url, transport, models, key in DEMO_PROVIDERS:
        record = engine.build_provider_record(provider_id=provider_id, label=label,
                                              base_url=base_url, models_url="",
                                              transport=transport)
        record["models"] = {name: {} for name in models}
        record["default_model"] = models[0]
        record["models_synced_at"] = stamp
        # 让截图里的余额卡片有内容：指向本机一个只返回演示数字的小接口。
        # 真实使用时这里接的是平台自己的余额接口（见 registry.py 的 balance 字段）。
        if provider_id == "deepseek":
            record["balance"] = {
                "kind": "json_path",
                "url": "http://127.0.0.1:%d/balance" % mock_port,
                "value_path": "data.balance",
                "currency_path": "data.currency",
                "label": "余额",
            }
            record["console_url"] = "https://platform.deepseek.com/usage"
        state_module.upsert_provider(state, record)
        secrets.store(provider_id, key)
    # 给 DeepSeek 设一个演示用的套餐额度，让“用量百分比”这张卡片有内容
    state["providers"]["deepseek"]["quota_tokens"] = 100_000_000
    state_module.save(state)
    engine.switch_to("deepseek", "deepseek-flash")

    # 造一份本机用量记录，这样进度条不是 0
    sessions = Path(workdir, "sessions", "2026", "09", "16")
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout-demo.jsonl").write_text(
        '{"type":"session_meta","payload":{"model_provider":"deepseek"}}\n'
        '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":'
        '{"input_tokens":11800000,"output_tokens":640000,"total_tokens":12440000}}}}\n',
        encoding="utf-8")

    # 预置一份“已是最新版本”的缓存，截图里就不会一直是空白
    from codex_switcher import __version__, paths as sw_paths, update as update_module
    sw_paths.ensure_dir(sw_paths.state_dir())
    sw_paths.state_dir().joinpath("update.json").write_text(json.dumps(
        {"status": "ok", "current": __version__, "latest": "v" + __version__,
         "up_to_date": True, "url": update_module.PROJECT_URL + "/releases"}, ensure_ascii=False))

    from codex_switcher.webui import server
    token = "screenshot"
    server.Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)

    base = "http://127.0.0.1:%d/?t=%s" % (port, token)
    # 无头 Chrome 的 navigator.language 是 en-US，不锁定语言的话截图会变成英文。
    url = base + "&lang=zh"
    target = ROOT / "docs" / "screenshot.png"
    if not shoot(chrome, url, target):
        print("截图失败", file=sys.stderr)
        httpd.shutdown()
        return 1
    print("已生成：%s" % target)

    # 英文界面来一张，给 README.en.md 用
    target_en = ROOT / "docs" / "screenshot.en.png"
    if shoot(chrome, base + "&lang=en", target_en):
        print("已生成：%s" % target_en)
    # 后面拍 GIF 用中文界面
    url = base + "&lang=zh"

    if "--gif" in sys.argv:
        frames_dir = Path(tempfile.mkdtemp(prefix="codex-switcher-gif-"))
        frames = []
        for index, (provider_id, model_id, view) in enumerate(GIF_STATES, 1):
            try:
                engine.switch_to(provider_id, model_id)
            except Exception as exc:                      # noqa: BLE001
                print("  跳过 %s：%s" % (provider_id, exc))
                continue
            frame = frames_dir / ("frame-%02d.png" % index)
            frame_url = url + ("&view=caps" if view == "caps" else "")
            if shoot(chrome, frame_url, frame):
                frames.append(frame)
                print("  拍到 %s · %s%s" % (provider_id, model_id,
                                            "（能力查看）" if view == "caps" else ""))
        gif = ROOT / "docs" / "demo.gif"
        if build_gif(frames, gif):
            print("已生成：%s（%.1f MB）" % (gif, gif.stat().st_size / 1048576))
        else:
            print("GIF 生成失败（需要 ffmpeg 且至少 2 帧）", file=sys.stderr)
        shutil.rmtree(frames_dir, ignore_errors=True)

    httpd.shutdown()
    return 0


def start_mock_balance_server() -> int:
    """截图专用的假余额接口：只监听本机，只返回演示数字。"""
    import json as _json
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):  # noqa: N802
            body = _json.dumps({"data": {"balance": "42.50", "currency": "CNY"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


if __name__ == "__main__":
    raise SystemExit(main())
