"""安全改写 ~/.codex/config.toml。

三条硬规则：
1. 只动模型相关字段和本工具管理的 model_providers 表，其余内容逐字节保留。
2. 写之前先备份，写之后用 TOML 解析器复验；不满足就整体放弃。
3. 原子替换，且在替换前确认磁盘内容仍是刚才读到的那份。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import paths

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None


class ConfigError(RuntimeError):
    pass


MANAGED_KEYS = (
    "model_provider",
    "model",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_supports_reasoning_summaries",
    "model_verbosity",
    "model_catalog_json",
    "model_context_window",
    "model_auto_compact_token_limit",
    "service_tier",
    "plan_mode_reasoning_effort",
    "review_model",
)

MANAGED_SET = frozenset(MANAGED_KEYS)


def require_toml() -> None:
    if tomllib is None:
        raise ConfigError(
            "当前 Python 没有 TOML 解析库。请升级到 Python 3.11+，"
            "或安装 tomli：python3 -m pip install tomli"
        )


def toml_available() -> bool:
    return tomllib is not None


def parse(text: str) -> Dict:
    require_toml()
    return tomllib.loads(text)


def read_top_level(text: str, keys) -> Dict:
    """读取文件头部的顶层键。

    有 TOML 库时走正规解析；没有时退化为逐行扫描（本工具写出的值都是
    JSON 兼容字面量，扫描足够可靠）。扫不动某个键就跳过，不抛错。
    """
    if toml_available():
        document = parse(text)
        return {key: document[key] for key in keys if key in document}
    wanted = set(keys)
    found: Dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        name = name.strip()
        if name not in wanted or name in found:
            continue
        try:
            found[name] = json.loads(raw.split(" #", 1)[0].strip())
        except json.JSONDecodeError:
            continue
    return found


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise ConfigError("不支持的配置值类型：%s" % type(value).__name__)


def _strip_top_level_model_keys(lines: List[str]) -> Tuple[List[str], int]:
    """删掉第一张表之前的所有模型相关键，返回 (剩余行, 首张表的下标)。"""
    split = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    kept: List[str] = []
    for line in lines[:split]:
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if match and match.group(1) in MANAGED_SET:
            continue
        kept.append(line)
    return kept, split


def rewrite_model_settings(text: str, settings: Dict) -> str:
    """把模型相关配置写到文件头部，其它内容原样保留。"""
    unknown = set(settings) - MANAGED_SET
    if unknown:
        raise ConfigError("不认识的配置项：%s" % ", ".join(sorted(unknown)))
    lines = text.splitlines(keepends=True)
    kept, split = _strip_top_level_model_keys(lines)
    head = "".join(kept).rstrip()
    body = "".join(lines[split:])
    rendered = "".join("%s = %s\n" % (key, toml_value(value)) for key, value in settings.items())
    new_text = (head + "\n\n" if head else "") + rendered + ("\n" + body if body else "")

    # 第一道校验：顶层模型键之外的文字必须逐字不变（任何 Python 版本都能做）
    def remainder(document: str) -> str:
        output = []
        for line in document.splitlines(keepends=True):
            if line.lstrip().startswith("["):
                output.append(line)
                continue
            if _is_managed_line(line):
                continue
            output.append(line)
        return "".join(output).strip()

    if remainder(text) != remainder(new_text):
        raise ConfigError("拒绝修改模型配置以外的内容")

    # 第二道校验：装了 TOML 库时再做一次语义比对
    if toml_available():
        before = parse(text)
        after = parse(new_text)
        protected_before = {k: v for k, v in before.items() if k not in MANAGED_SET}
        protected_after = {k: v for k, v in after.items() if k not in MANAGED_SET}
        if protected_before != protected_after:
            raise ConfigError("拒绝修改模型配置以外的内容")
        if {k: after[k] for k in MANAGED_SET if k in after} != settings:
            raise ConfigError("模型配置校验未通过")
    else:
        if read_top_level(new_text, tuple(settings.keys())) != settings:
            raise ConfigError("模型配置校验未通过")
    return new_text


def _is_managed_line(line: str) -> bool:
    match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
    return bool(match and match.group(1) in MANAGED_SET)


_TABLE_RE = re.compile(r"^[ \t]*(\[\[[^\]\n]+\]\]|\[[^\]\n]+\])[ \t]*(?:#.*)?$")


def _table_blocks(lines: List[str]) -> List[Tuple[str, int, int]]:
    """列出所有表名及其行区间 [start, end)。"""
    blocks: List[Tuple[str, int, int]] = []
    current_name: Optional[str] = None
    current_start = 0
    in_multiline: Optional[str] = None
    for index, line in enumerate(lines):
        # 跳过三引号多行字符串内部的“[表名]”，避免误判
        if in_multiline is not None:
            if in_multiline in line:
                in_multiline = None
            continue
        stripped = line.split("#", 1)[0]
        if stripped.count('"""') % 2 == 1:
            in_multiline = '"""'
            continue
        if stripped.count("'''") % 2 == 1:
            in_multiline = "'''"
            continue
        match = _TABLE_RE.match(line)
        if not match:
            continue
        if current_name is not None:
            blocks.append((current_name, current_start, index))
        raw = match.group(1)
        current_name = raw if raw.startswith("[[") else raw[1:-1].strip()
        current_start = index
    if current_name is not None:
        blocks.append((current_name, current_start, len(lines)))
    return blocks


def _render_table(name: str, values: Dict) -> str:
    body = ["[%s]\n" % name]
    for key, value in values.items():
        body.append("%s = %s\n" % (key, toml_value(value)))
    return "".join(body)


def upsert_provider_block(
    text: str,
    provider_id: str,
    fields: Dict,
    auth: Optional[Dict] = None,
) -> str:
    """写入或替换 [model_providers.<id>] 及其 .auth 子表。"""
    base_name = "model_providers." + provider_id
    auth_name = base_name + ".auth"
    desired = {
        base_name: _render_table(base_name, fields),
        auth_name: _render_table(auth_name, auth) if auth else None,
    }

    lines = text.splitlines(keepends=True)
    blocks = _table_blocks(lines)
    remove: List[Tuple[int, int]] = []
    for name, start, end in blocks:
        if name in desired:
            remove.append((start, end))

    # 从后往前删，保证下标不失效
    for start, end in sorted(remove, reverse=True):
        del lines[start:end]

    new_text = "".join(lines)
    existing = {name for name, _, _ in _table_blocks(new_text.splitlines(keepends=True))}
    appended = []
    for name in (base_name, auth_name):
        if name in desired and desired[name] is not None and name not in existing:
            appended.append(desired[name])
    if appended:
        new_text = new_text.rstrip("\n") + "\n\n" + "\n".join(part.rstrip("\n") + "\n" for part in appended)

    target_names = {base_name} | ({auth_name} if auth else set())
    if _text_without_blocks(text, target_names).strip() != _text_without_blocks(new_text, target_names).strip():
        raise ConfigError("写入服务商配置时改动了不该改的内容")

    if toml_available():
        after = parse(new_text)
        providers = after.get("model_providers", {})
        check = providers.get(provider_id) or {}
        for key, value in fields.items():
            if check.get(key) != value:
                raise ConfigError("服务商配置校验未通过：%s" % key)
        if auth:
            actual_auth = check.get("auth") or {}
            for key, value in auth.items():
                if actual_auth.get(key) != value:
                    raise ConfigError("凭据助手配置校验未通过：%s" % key)
            for conflicting in ("env_key", "experimental_bearer_token", "requires_openai_auth"):
                if conflicting in check:
                    raise ConfigError("检测到冲突的认证配置：%s" % conflicting)
    else:
        for key, value in fields.items():
            if ("%s = %s" % (key, toml_value(value))) not in new_text:
                raise ConfigError("服务商配置校验未通过：%s" % key)
    return new_text


def _text_without_blocks(text: str, names) -> str:
    """去掉指定表的内容，用于比对“其余部分有没有被动过”。"""
    names = set(names)
    lines = text.splitlines(keepends=True)
    covered = set()
    for name, start, end in _table_blocks(lines):
        if name in names:
            covered.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in covered)


def remove_provider_block(text: str, provider_id: str) -> str:
    base_name = "model_providers." + provider_id
    targets = {base_name, base_name + ".auth"}
    lines = text.splitlines(keepends=True)
    remove = [(start, end) for name, start, end in _table_blocks(lines) if name in targets]
    for start, end in sorted(remove, reverse=True):
        del lines[start:end]
    return "".join(lines)


def backup(path: Path) -> Path:
    target_dir = paths.ensure_dir(paths.backups_dir())
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "%s-%s-%d%s" % (path.stem, stamp, time.time_ns(), path.suffix or ".toml")
    target = target_dir / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(path.read_text())
    return target


def atomic_write(path: Path, text: str, expected: str) -> None:
    if path.exists() and path.read_text() != expected:
        raise ConfigError("配置文件在本次操作期间被其他程序修改，请重新运行")
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_settings(config: Path) -> Dict:
    return read_top_level(config.read_text(), MANAGED_KEYS)
