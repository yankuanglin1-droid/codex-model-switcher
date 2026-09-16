# 安全说明

## 密钥去哪了

**API Key 永远不会写进 `~/.codex/config.toml`。**

配置文件里只出现一个凭据助手命令：

```toml
[model_providers.deepseek.auth]
command = "~/.codex/bin/codex-provider-keychain.py"
args = ["deepseek"]
timeout_ms = 10000
refresh_interval_ms = 300000
```

Codex 每次请求时执行它，助手从系统钥匙串取出密钥写到 stdout，
其余任何信息都走 stderr，避免污染返回值。

存储位置优先级：

| 系统 | 存储 | 说明 |
| --- | --- | --- |
| macOS | 登录钥匙串 | 服务名 `com.codex.model-switcher.<平台ID>` |
| Linux | Secret Service | 需要 `secret-tool`（`libsecret-tools`） |
| 都没有 | `~/.codex/model-switcher/credentials/<平台ID>.key` | 权限 0600，会在 `doctor` 里明确提示保护更弱 |

## 写命令行参数的风险

`codex-switcher add --key sk-xxx` 会让密钥短暂出现在 **进程列表** 和 **shell 历史** 里。

所以推荐两种更安全的方式：

```bash
echo "$KEY" | codex-switcher add --preset deepseek --key-stdin   # 从标准输入
codex-switcher app                                              # 在图形界面里粘贴
```

## 脱敏

- `codex-switcher list` 只显示 `sk-abc…wxyz` 形式。
- 图形界面不提供任何"显示完整密钥"的接口。
- 协议桥不落盘、不打印密钥，只把它从 Codex 的请求头转发到上游。
- 日志文件（`~/.codex/model-switcher/bridge.err.log`）不会包含密钥。

## 配置文件保护

每次写入都执行四步：

1. **备份**到 `~/.codex/model-switcher/backups/`（权限 0600）。
2. **只改该改的**：模型相关顶层键 + 本工具管理的 `[model_providers.<id>]` 块。
3. **校验**：逐行确认其它内容一字未动；如果 Python 有 TOML 解析库，再做一次语义比对
   （两棵树除模型字段外必须完全相等）。
4. **原子替换**：临时文件 + `os.replace`，并在替换前确认磁盘内容仍是刚读到的那份，
   防止和别的程序并发写冲突。

任何一步不满足，整个操作作废，配置文件保持原样。

## 网络暴露面

| 组件 | 监听地址 | 认证 |
| --- | --- | --- |
| 图形界面 | `127.0.0.1:<随机端口>` | 每次启动生成一次性令牌，URL 里必须带对 |
| 协议桥 | `127.0.0.1:8787` | 无（仅本机），密钥由请求头透传 |

两者都不监听外网地址，也不会被局域网内其它设备访问到。

协议桥不做任何持久化：不存密钥、不存请求内容、不写访问日志。

## 本机用量统计

`codex-switcher balance` 中的"本机用量"读取 `~/.codex/sessions/**/*.jsonl`，
只解析两类字段：

- `session_meta.payload.model_provider`（这条会话属于哪个平台）
- `event_msg.payload.info.total_token_usage.*`（token 计数）

不读取、不保存、不外传对话内容。统计数据只在本机内存里聚合，带 60 秒缓存。

## 发布前自检

仓库自带扫描脚本，检查是否有密钥或个人路径被误提交：

```bash
python3 tools/scan_secrets.py
```

它会检查 OpenAI / MiniMax / Anthropic / OpenRouter / 智谱 的密钥样式、
GitHub Token、私钥块，以及 `/Users/<名字>/`、`/home/<名字>/` 这类个人路径。

`.gitignore` 排除了 `providers.json`、`catalogs/`、`credentials/`、`backups/`、
`config.toml` 等运行时产物，确保本机状态不会被提交。
