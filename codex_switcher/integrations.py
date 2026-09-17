"""平台全量能力的同步配置。

「配好一个 API」不只是让 Codex 能对话：同一把 Key 还能生图、生视频、
语音合成、联网搜索，平台官方的 MCP Server / CLI 也认这把钥匙。
用户要求的是「配 API 时把这些一起配好」，所以这个模块负责两件事：

  · MCP      —— 把平台官方 MCP Server 写进 Codex 的 [mcp_servers.<id>]，
                这样 Codex 里就能直接调用生图 / 生视频 / 语音等工具，
                而不是只停在「文档里说有」
  · CLI/脚本 —— 写一份 0600 的环境变量文件
                （~/.codex/model-switcher/env/<id>.sh），
                source 一下，官方 CLI、curl 脚本、SDK 全都能直接用

安全规则与 configfile 一致：写前备份、写后用 TOML 解析器复验、
原子替换、其余内容逐字节不动。密钥只写进 0600 的文件，不进状态目录之外。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Dict, List, Optional

from . import capabilities as capabilities_module
from . import configfile
from . import paths
from . import secrets as secrets_module

ENV_SUBDIR = "env"


def _safe_env_name(provider_id: str) -> str:
    """把平台 id 变成合法的环境变量前缀：minimax-2 -> MINIMAX_2。"""
    return (provider_id or "provider").upper().replace("-", "_").replace(".", "_")


def mcp_item(provider_id: str) -> Optional[Dict]:
    """这个平台有没有可自动配置的官方 MCP Server。"""
    for item in capabilities_module.platform_surface(provider_id):
        if item.get("key") == "mcp" and item.get("status") == "yes":
            config = item.get("mcp_config")
            if isinstance(config, dict) and config.get("command"):
                return dict(item, mcp_config=config)
    return None


def env_file(provider_id: str) -> Path:
    return paths.state_dir() / ENV_SUBDIR / ("%s.sh" % (provider_id or "provider"))


def render_env_profile(provider_id: str, record: Dict, api_key: str) -> str:
    """环境变量文件内容。官方 CLI / curl / SDK 直接 source 就能用。"""
    prefix = _safe_env_name(provider_id)
    base_url = (record.get("upstream_base_url") or record.get("base_url") or "").rstrip("/")
    lines = [
        "# codex-model-switcher 生成的平台环境（%s）" % provider_id,
        "# 用法：source %s" % env_file(provider_id),
        "# 本文件 0600 权限，含密钥，不要提交到任何仓库。",
        "",
        "export %s_API_KEY=%s" % (prefix, _sh(api_key)),
        "export %s_BASE_URL=%s" % (prefix, _sh(base_url)),
    ]
    # 平台文档里认的正式变量名（如 MINIMAX_API_KEY）也一并导出，
    # 官方 SDK 大多认自己的名字而不是我们的前缀
    item = mcp_item(provider_id)
    if item:
        env_key = (item["mcp_config"].get("env_key") or "").strip()
        if env_key and env_key != "%s_API_KEY" % prefix:
            lines.append("export %s=%s" % (env_key, _sh(api_key)))
        for name, value in (item["mcp_config"].get("extra_env") or {}).items():
            if str(name).strip():
                lines.append("export %s=%s" % (name, _sh(str(value))))
    if base_url:
        lines.append("export %s_API_HOST=%s" % (prefix, _sh(base_url)))
    lines.append("")
    return "\n".join(lines)


def _sh(value: str) -> str:
    """单引号包裹；单引号本身翻倍。空值返回空串。"""
    return "'" + (value or "").replace("'", "'\\''") + "'"


def _mcp_table_name(provider_id: str) -> str:
    return "mcp_servers." + (provider_id or "provider")


def _render_mcp_table(provider_id: str, config: Dict, api_key: str) -> str:
    """渲染 [mcp_servers.<id>] 表。env 渲染成 TOML 内联表。"""
    name = _mcp_table_name(provider_id)
    lines = ["[%s]\n" % name]
    lines.append("command = %s\n" % configfile.toml_value(str(config["command"])))
    args = [str(item) for item in (config.get("args") or [])]
    if args:
        lines.append("args = %s\n" % configfile.toml_value(args))
    env = dict(config.get("extra_env") or {})
    env_key = (config.get("env_key") or "").strip()
    if env_key:
        env[env_key] = api_key
    if env:
        inner = ", ".join("%s = %s" % (key, configfile.toml_value(str(value)))
                          for key, value in env.items())
        lines.append("env = { %s }\n" % inner)
    return "".join(lines)


def _upsert_mcp_block(provider_id: str, table_text: Optional[str]) -> Path:
    """写入 / 删除 / 替换 [mcp_servers.<id>]。table_text 为 None 表示删除。

    流程与 configfile.upsert_provider_block 相同：块级替换 + 逐字校验 +
    语义复验 + 备份 + 原子写。
    """
    config = paths.config_path()
    if not config.exists():
        raise configfile.ConfigError("找不到 Codex 配置文件：%s" % config)
    text = config.read_text()
    name = _mcp_table_name(provider_id)

    lines = text.splitlines(keepends=True)
    blocks = configfile._table_blocks(lines)
    remove = [(start, end) for block_name, start, end in blocks if block_name == name]
    for start, end in sorted(remove, reverse=True):
        del lines[start:end]
    new_text = "".join(lines)
    if table_text:
        new_text = new_text.rstrip("\n") + "\n\n" + table_text.rstrip("\n") + "\n"

    # 其余内容必须逐字不变
    targets = {name}
    if configfile._text_without_blocks(text, targets).strip() != \
            configfile._text_without_blocks(new_text, targets).strip():
        raise configfile.ConfigError("写入 MCP 配置时改动了不该改的内容")
    # 内容已经一致就别再动文件：切换是高频操作，每次都写会白白刷 mtime、
    # 堆一目录毫无意义的备份
    if new_text == text:
        return None
    if configfile.toml_available():
        after = configfile.parse(new_text)
        block = (after.get("mcp_servers") or {}).get(provider_id) or {}
        if table_text:
            if not block.get("command"):
                raise configfile.ConfigError("MCP 配置校验未通过：command")
        # 删除时确认真的删掉了
        elif block:
            raise configfile.ConfigError("MCP 配置删除校验未通过")
    backup = configfile.backup(config)
    configfile.atomic_write(config, new_text, text)
    return backup


def sync(provider_id: str, record: Dict, api_key: Optional[str] = None) -> Dict:
    """把一个平台的全量能力调用环境配好。返回做了什么的清单。

    任何一步失败都只记录、不抛错 —— 调用方（add / switch）的主流程
    不能因为周边配置失败而中断。
    """
    record = record or {}
    result: Dict = {"provider": provider_id, "mcp": None, "env_file": None,
                    "errors": []}
    key = api_key or secrets_module.load(provider_id)

    item = mcp_item(provider_id)
    if item and key:
        try:
            config = item["mcp_config"]
            table = _render_mcp_table(provider_id, config, key)
            backup = _upsert_mcp_block(provider_id, table)
            result["mcp"] = {"command": config["command"],
                             "args": list(config.get("args") or []),
                             "env_key": config.get("env_key"),
                             "table": _mcp_table_name(provider_id),
                             "backup": str(backup) if backup else None}
        except Exception as exc:  # noqa: BLE001 - 周边配置失败不影响主流程
            result["errors"].append("mcp: %s" % exc)

    if key:
        try:
            path = env_file(provider_id)
            paths.ensure_dir(path.parent)
            text = render_env_profile(provider_id, record, key)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            result["env_file"] = str(path)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append("env: %s" % exc)
    return result


def remove(provider_id: str) -> Dict:
    """删平台时把 MCP 配置和环境文件一起清掉。"""
    result: Dict = {"provider": provider_id, "mcp_removed": False,
                    "env_removed": False, "errors": []}
    try:
        _upsert_mcp_block(provider_id, None)
        result["mcp_removed"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("mcp: %s" % exc)
    try:
        path = env_file(provider_id)
        if path.exists():
            path.unlink()
            result["env_removed"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("env: %s" % exc)
    return result


def status(provider_id: str) -> Dict:
    """这个平台的全量能力环境配好了没。"""
    result = {"mcp": False, "env_file": None, "available": bool(mcp_item(provider_id))}
    config = paths.config_path()
    if config.exists():
        try:
            document = configfile.parse(config.read_text())
            block = (document.get("mcp_servers") or {}).get(provider_id)
            result["mcp"] = bool(block and block.get("command"))
        except Exception:  # noqa: BLE001 - 解析不了就按没配置算
            result["mcp"] = False
    path = env_file(provider_id)
    if path.exists():
        result["env_file"] = str(path)
    return result


def surface_with_labels(provider_id: str) -> List[Dict]:
    """给界面用的能力面：每项带上中文标签。"""
    items = []
    for item in capabilities_module.platform_surface(provider_id):
        row = dict(item)
        row["label"] = capabilities_module.SURFACE_LABELS.get(item.get("key"), item.get("key"))
        row["mcp_ready"] = False
        items.append(row)
    return items
