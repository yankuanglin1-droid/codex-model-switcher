"""端到端链路检查：回答「Codex 现在真的能跟这个平台说话吗」。

为什么需要这一层：之前每一处检查都是**局部**的 —— 配置写对了、密钥存进
钥匙串了、平台加好了，每一项单独看都"没问题"，但 Codex 一发请求就炸。
用户看到的现象有三张脸：

  1. 页面加载不出来 / 一直转圈
  2. 发消息反复「正在尝试重新连接」
  3. 报「ChatGPT 订阅无法使用 xxx（第三方模型）」

第 3 条最能说明问题：Codex 只有在**拿不到第三方平台的密钥**时才会退回
ChatGPT 订阅那条鉴权通路，然后把第三方模型名发到官方后端，被拒。所以
「配好了」和「能连通」之间隔着好几道坎，任何一道塌了都是上面那几张脸。

这里把整条链路串起来检查，且**凭据助手那一环是真跑一遍** —— 只检查文件
存在是不够的：文件在、但她bang 指向的解释器是坏的，照样取不到密钥。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, List, Optional

from . import paths

# 状态：坏 / 提醒 / 好
FAIL = "fail"
WARN = "warn"
OK = "ok"

# 跑凭据助手的超时。Codex 那边配的是 10 秒（refresh_interval 5 分钟），
# 这里稍微放宽一点，避免慢机器上误报。
HELPER_TIMEOUT = 15

# 这些键一旦出现在 provider 块里，Codex 就不会用我们的凭据助手取密钥，
# 而是走 ChatGPT 订阅鉴权 —— 正是「订阅无法使用第三方模型」的直接来源。
CONFLICTING_AUTH_KEYS = ("requires_openai_auth", "env_key", "experimental_bearer_token")


def _item(status: str, name: str, detail: str, fix: str = "") -> Dict:
    return {"status": status, "name": name, "detail": detail, "fix": fix}


def _top_level(text: str) -> Dict:
    """读 config.toml 顶层键值。拿不到 TOML 库就退回逐行读。

    这里只读顶层，不解析整份文件：用户的配置可能有几千行插件段，
    任何解析异常都不该让检查本身崩掉。
    """
    try:
        from . import configfile
        if configfile.toml_available():
            return configfile.parse(text)
    except Exception:  # noqa: BLE001
        pass
    result = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            if stripped.startswith("["):
                break
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip().strip("\"'")
    return result


def _provider_block(text: str, provider_id: str) -> Optional[Dict]:
    """取 [model_providers.<id>] 段。找不到返回 None。"""
    try:
        from . import configfile
        if configfile.toml_available():
            document = configfile.parse(text)
            providers = document.get("model_providers") or {}
            block = providers.get(provider_id)
            return dict(block) if isinstance(block, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return _provider_block_by_scan(text, provider_id)


def _provider_block_by_scan(text: str, provider_id: str) -> Optional[Dict]:
    """没有 TOML 库时的兜底：逐行扫那一段。"""
    header = "[model_providers.%s]" % provider_id
    auth_header = header + ".auth"
    block: Dict = {}
    auth: Dict = {}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == header:
            section = "main"
            continue
        if stripped == auth_header:
            section = "auth"
            continue
        if stripped.startswith("["):
            section = None
            continue
        if section and "=" in stripped:
            key, _, value = stripped.partition("=")
            target = auth if section == "auth" else block
            target[key.strip()] = value.strip().strip("\"'")
    if not block:
        return None
    if auth:
        block["auth"] = auth
    return block


def _run_helper(provider_id: str) -> Dict:
    """真跑一次凭据助手。这是整条链路里最容易塌、也最容易被忽略的一环。

    只检查"文件在不在"是没用的：v1.6.9 修的那个 bug 就是文件在、
    shebang 指向的解释器是坏的，进程一起来就 SIGABRT，Codex 拿到空密钥。
    """
    # 先问「Codex 到底会怎么调它」，再检查那个东西。顺序不能反：
    # 直接查 paths.keychain_helper() 会漏掉 Windows 上"用解释器跑 .py"
    # 这种形式，也会把本来能用的调用方式误判成缺失。
    try:
        from . import secrets
        command, arguments = secrets.helper_command(provider_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": "无法确定凭据助手的调用方式：%s" % exc,
                "fix": "执行 codex-switcher init"}

    helper = paths.keychain_helper()
    if not str(command).startswith("<") and not os.path.exists(str(command)):
        return {"ok": False, "detail": "凭据助手不存在：%s" % command,
                "fix": "执行 codex-switcher init 重新生成"}
    if helper.exists() and not os.access(str(helper), os.X_OK):
        return {"ok": False, "detail": "凭据助手没有执行权限：%s" % helper,
                "fix": "执行 chmod +x %s" % helper}

    try:
        done = subprocess.run([command] + [str(a) for a in arguments],
                              capture_output=True, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "detail": "凭据助手 %d 秒没返回（Codex 那边只等 10 秒）" % HELPER_TIMEOUT,
                "fix": "多半是钥匙串被锁住或弹窗等待输入；先解锁钥匙串再试"}
    except OSError as exc:
        return {"ok": False, "detail": "凭据助手无法启动：%s" % exc,
                "fix": "检查 %s 的第一行（shebang）指向的解释器是否存在" % helper}

    if done.returncode != 0:
        stderr = (done.stderr or b"").decode("utf-8", "replace").strip()
        # 退出码非 0 时 stderr 才是线索；但里面可能有密钥片段，只取第一行且截断
        hint = stderr.splitlines()[0][:200] if stderr else "没有输出"
        return {"ok": False,
                "detail": "凭据助手退出码 %d：%s" % (done.returncode, hint),
                "fix": "执行 %s %s 看完整报错" % (helper, provider_id)}
    key = (done.stdout or b"").decode("utf-8", "replace").strip()
    if not key:
        return {"ok": False,
                "detail": "凭据助手返回空（没崩，但也没给密钥）",
                "fix": "重新添加密钥：codex-switcher add --id %s --key-stdin" % provider_id}
    return {"ok": True, "detail": "取到密钥（长度 %d，内容不显示）" % len(key)}


def check(provider_id: Optional[str] = None, check_history: bool = False) -> Dict:
    """跑完整条链路。返回 {ok, provider, checks:[...]}。"""
    config = paths.config_path()
    checks: List[Dict] = []

    if not config.exists():
        return {"ok": False, "provider": None, "checks": [
            _item(FAIL, "配置文件", "没有找到 %s" % config,
                  "先运行一次 Codex，让它生成配置文件")]}

    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "provider": None, "checks": [
            _item(FAIL, "配置文件", "读不到：%s" % exc, "检查文件权限")]}

    top = _top_level(text)
    active = provider_id or top.get("model_provider") or "openai"
    model = top.get("model") or ""
    checks.append(_item(OK, "当前平台", "%s · %s" % (active, model or "未指定模型")))

    # ---- 1. provider 块必须存在 --------------------------------------
    # 缺了这一段，Codex 根本不认识这个平台，会退回 ChatGPT 订阅鉴权，
    # 于是报「订阅无法使用第三方模型」。
    block = _provider_block(text, active)
    if block is None:
        checks.append(_item(FAIL, "平台配置段",
                            "config.toml 里没有 [model_providers.%s]" % active,
                            "重新切换一次：codex-switcher use %s" % active))
        return {"ok": False, "provider": active, "checks": checks}
    checks.append(_item(OK, "平台配置段", "[model_providers.%s] 存在" % active))

    # ---- 2. 不能有冲突的认证键 ---------------------------------------
    conflicts = [key for key in CONFLICTING_AUTH_KEYS if key in block]
    if conflicts:
        checks.append(_item(FAIL, "认证方式",
                            "平台段里有冲突的键：%s —— Codex 会因此改用 ChatGPT 订阅鉴权"
                            % "、".join(conflicts),
                            "执行 codex-switcher use %s 重写这一段" % active))
    else:
        checks.append(_item(OK, "认证方式", "使用凭据助手取密钥"))

    # ---- 3. 地址 -----------------------------------------------------
    base_url = block.get("base_url") or ""
    if not base_url:
        checks.append(_item(FAIL, "平台地址", "base_url 是空的",
                            "重新添加平台：codex-switcher add --id %s --base-url <地址>" % active))
    else:
        checks.append(_item(OK, "平台地址", base_url))

    # ---- 4. 走协议桥的话，桥必须在跑 ---------------------------------
    # 桥没跑的时候 base_url 指向 127.0.0.1，Codex 连不上，表现就是
    # 「加载不出来 / 反复重新连接」。
    if "127.0.0.1" in base_url or "localhost" in base_url:
        port = _port_from_url(base_url)
        try:
            from . import bridge as bridge_module
            running = bridge_module.is_running(port) if port else False
        except Exception:  # noqa: BLE001
            running = False
        if running:
            checks.append(_item(OK, "本地协议桥", "正在监听 %s" % (port or base_url)))
        else:
            checks.append(_item(FAIL, "本地协议桥",
                                "这个平台走本地协议桥，但桥没有运行（%s）" % base_url,
                                "执行 codex-switcher bridge --install-agent 设为开机自启，"
                                "或先手动跑 codex-switcher bridge"))

    # ---- 5. 密钥 -----------------------------------------------------
    try:
        from . import secrets
        has_key = bool(secrets.load(active))
    except Exception:  # noqa: BLE001
        has_key = False
    if has_key:
        checks.append(_item(OK, "密钥存储", "钥匙串里有 %s 的密钥" % active))
    else:
        checks.append(_item(FAIL, "密钥存储", "没有 %s 的密钥" % active,
                            "codex-switcher add --id %s --key-stdin" % active))

    # ---- 6. 凭据助手真跑一遍 -----------------------------------------
    # 最关键的一环。文件在、密钥也在，但脚本跑不起来 —— 那 Codex 照样
    # 拿不到密钥，然后退回订阅鉴权，报「订阅无法使用第三方模型」。
    if has_key:
        outcome = _run_helper(active)
        checks.append(_item(OK if outcome["ok"] else FAIL, "凭据助手",
                            outcome["detail"], outcome.get("fix", "")))

    # ---- 7. 模型在目录里吗 -------------------------------------------
    if model:
        try:
            from . import catalog as catalog_module
            known = catalog_module.load(catalog_module.catalog_path(active)) \
                if hasattr(catalog_module, "load") else None
            names = list((known or {}).get("models", {}).keys()) if known else []
            if names and model not in names:
                checks.append(_item(WARN, "模型清单",
                                    "目录里没有 %s（共 %d 个模型）" % (model, len(names)),
                                    "刷新模型：codex-switcher refresh --provider %s" % active))
        except Exception:  # noqa: BLE001
            pass

    # ---- 8. 会话历史（可选，慢）--------------------------------------
    if check_history:
        try:
            from . import history as history_module
            scan = history_module.scan(limit=40)
            if scan.get("dirty"):
                checks.append(_item(WARN, "会话历史",
                                    "%d 个会话含会被第三方拒绝的条目" % len(scan["dirty"]),
                                    "执行 codex-switcher history --sweep --cross-provider"))
            else:
                checks.append(_item(OK, "会话历史", "没有发现会被拒收的条目"))
        except Exception:  # noqa: BLE001
            pass

    failed = [item for item in checks if item["status"] == FAIL]
    return {"ok": not failed, "provider": active, "checks": checks}


def _port_from_url(url: str) -> Optional[int]:
    import re
    match = re.search(r":(\d+)", url)
    return int(match.group(1)) if match else None


def describe(report: Dict) -> str:
    """给人看的一句话结论。"""
    if report["ok"]:
        return "链路检查通过：%s 可以正常请求。" % report.get("provider")
    failed = [item for item in report["checks"] if item["status"] == FAIL]
    return "有 %d 处会挡住请求：%s" % (
        len(failed), "；".join(item["name"] for item in failed))


def to_text(report: Dict) -> str:
    marks = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for item in report["checks"]:
        line = "  %s %s：%s" % (marks.get(item["status"], "?"), item["name"], item["detail"])
        if item["fix"] and item["status"] != OK:
            line += "\n      修法：%s" % item["fix"]
        lines.append(line)
    return "\n".join(lines)
