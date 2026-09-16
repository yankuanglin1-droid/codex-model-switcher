# 让 AI 帮你配置（AI 执行手册）

这份文档给 **AI Agent** 看。人只需要把下面这段话发给 AI：

> 请阅读 `docs/ai-setup.md`，按里面的流程帮我把第三方模型接进 Codex。

---

## AI 必须问到的信息

信息不齐不要开始，逐项问清楚：

1. **平台名称**（例如 `DeepSeek`、`MiniMax`、`我的中转站`）
2. **Base URL**（用户不知道时，按平台名去官方文档查；不要凭印象编）
3. **API Key**（告诉用户：通过标准输入传入，不要贴在公开渠道）
4. **指定模型**（可选；不指定就自动拉取全部）
5. **是否需要余额显示**（可选；需要的话问平台是否提供了余额接口地址）

## 执行步骤

```bash
# 1. 安装
git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
cd codex-model-switcher
bash install.sh

# 2. 自检，确认环境干净
codex-switcher doctor

# 3. 添加平台（密钥走 stdin，不写进任何文件）
printf '%s' "$KEY" | codex-switcher add \
  --name "<平台名>" \
  --base-url "<Base URL>" \
  --key-stdin

# 4. 检查拉到的模型是否符合预期
codex-switcher list
codex-switcher models <平台ID>

# 5. 切换
codex-switcher use <平台ID>

# 6. 如果部分平台走协议桥，把它常驻
codex-switcher bridge --install-agent     # macOS
codex-switcher bridge                     # 其它系统，前台常驻或用系统服务托管

# 7. 复验
codex-switcher doctor
```

## 必须告知用户的三件事

1. **完全退出并重新打开 Codex（⌘Q）**，只关窗口不生效。
2. **旧任务不会跟着切换**：旧对话在创建时就绑定了原服务商，在里面选新平台的模型会报
   `model is not supported when using Codex with a ChatGPT account`。
   要继续旧内容请「分叉」或新建任务。
3. **怎么切回官方**：`codex-switcher restore`，然后同样重启 Codex。

## 硬性约束

- **不要**把 API Key 写进 `config.toml`、README、聊天记录、任何脚本或提交。
- **不要**手工编辑 `~/.codex/config.toml` 来绕过工具：`wire_api` 必须保持 `responses`，
  写 `chat` 会让 Codex 直接拒绝启动。
- **不要**把 provider id 起成 `openai`、`ollama`、`lmstudio`、`codex`、`azure`
  ——这些是 Codex 保留名，用它们会导致模型列表被忽略。
- **不要**在没有告诉用户的情况下删改他们的插件、MCP、Skills 配置。
  工具本身就只碰模型相关字段；手工操作也必须遵守。
- **不要**声称某个平台"额度可以查"除非真的调通了接口。查不到就照实说。

## 出问题时

按顺序收集证据，不要瞎猜：

```bash
codex-switcher doctor          # 环境、密钥、模型、协议桥四类检查
codex-switcher export          # 脱敏状态，可直接贴给用户或写进 issue
codex-switcher status          # 当前生效的平台与模型
codex-switcher models <平台ID>  # 模型清单是否正常
```

常见根因，按出现频率排序：

1. 没有完全退出重启 Codex。
2. 在旧任务里尝试换模型（服务商绑定问题）。
3. provider id 撞了保留名。
4. 需要协议桥的平台没启动桥。
5. 平台账号本身没额度（例如智谱返回 `insufficient_quota`）。
6. 本机代理 / hosts 把域名指向了 `127.0.0.1`（和平台无关）。

## 验证而不是假设

如果你要宣称"配置成功"，至少要拿到这些证据：

- `codex-switcher status` 显示的 provider 与 model 是用户要的那一套
- `codex-switcher models <平台ID>` 能列出模型
- `codex-switcher doctor` 没有报错
- 用户重启 Codex 后，模型列表里能看到目标模型

前三项你能自己验证；最后一项需要用户配合确认。不要替用户宣布成功。
