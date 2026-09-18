# codex（ChatGPT App）多平台模型切换

[English](README.md) · **中文** · [让 AI 帮我装 →](INSTALL-WITH-AI.md)

把 **任何提供 OpenAI 兼容 API 的平台** 接进 ChatGPT App 里的 Codex —— DeepSeek、
MiniMax、智谱 GLM、Kimi、通义、硅基流动、OpenRouter、Groq，你自己搭的中转站，
本机的 Ollama —— 全部出现在 Codex 输入框旁的模型列表里，随时一键切回官方 OpenAI。

> **不是只会列表里那几家。** 只要平台对外提供 OpenAI 兼容接口就能接：
> 官方 API、第三方中转、自建网关、本地模型，一视同仁。
> 要准备的只有三样：平台名、Base URL、API Key。

![界面总览](docs/screenshot.png)

<sub>截图来自演示脚本造的数据（假 Key + 本机假余额接口），不是谁的真实账号。
界面支持中文 / English 一键切换。</sub>

<p>
  <img src="docs/demo.gif" alt="切换平台、查看能力、切回官方" width="49%">
  <img src="docs/showcase-caps.png" alt="能力查看：选中模型到底能做什么" width="49%">
</p>

---

## 它解决什么问题

手改 `~/.codex/config.toml` 看似可行，坑都在后面：新版 Codex 直接拒绝
`wire_api = "chat"`；模型目录 JSON 要手写，错一个字段模型列表就不显示；API Key 明文
躺在配置里；而且**旧对话把服务商写死在会话文件里**，切完平台一继续任务就报
`unknown model`。这个工具把一整套流程做成两条命令，并把踩过的每个坑固化成代码。

## 和同类做法的对比

| | 手改 config.toml | 单平台封装脚本 | **本工具** |
| --- | --- | --- | --- |
| 支持的平台 | 会配的都能用 | 通常只有一家 | **任何 OpenAI 兼容 API**（官方 / 中转 / 本地） |
| 模型列表 | 手写 JSON，易错 | 固定 | **自动拉取**生成目录，随时刷新 |
| 协议 | 必须 `responses`，否则拒绝启动 | 不一定 | 永远写 `responses`；只支持 Chat 的平台走**内置协议桥** |
| API Key | 明文进配置 | 不一 | **只进系统钥匙串**（Keychain / Secret Service / DPAPI） |
| 旧对话 | 切换后报错 | 不处理 | 切换时**自动把最近任务搬过去** + 后台巡检自愈 |
| 上下文 | 靠猜 | 不处理 | **三层守卫**：官方窗口 → Codex 可用 → 自动压缩线，超限一键「换成装得下的模型」 |
| 模型能力 | 靠猜 | 不处理 | **真实请求实测**（读图 / 思考 / 工具），结论写回目录 |
| 历史兼容 | 不处理 | 不处理 | 全量清扫每个会话文件：剥掉缺 `call_id` 的孤儿输出（400 元凶），真搬家时再剥 OpenAI 专有条目，全程备份 |
| 切回官方 | 手工改回 | 常常做不到 | `codex-switcher restore`，第三方配置保留 |

## 功能

- **一键接入**：17 个内置预设，也能手动填任意 Base URL（中转站、自建网关都行）。
- **全部模型可选**：自动拉取完整清单生成 Codex 模型目录。
- **一键切换 / 还原**：命令行、菜单、图形界面三种方式。切换过程有全屏加载
  反馈（含旧任务迁移，耗时明确告知），不会误以为卡死。
- **适配新版 ChatGPT.app**：Codex 桌面端已并入 `ChatGPT.app`（bundle id 仍是
  `com.openai.codex`），本工具按 bundle id 识别，「一键重启」对 Codex.app 与
  ChatGPT.app 两种形态都有效。
- **切回官方 = 恢复原生订阅**：`restore` / 界面「恢复官方」只写回
  `model_provider = openai` 与官方模型名，清掉第三方模型目录引用，
  Codex 恢复显示你账号自己的模型列表——官方不提供可选清单，也不该有。
- **旧任务全跟着走**：切换时把最近在用的任务整体搬到新平台（会话文件 + 两个数据库，
  先备份）；其余任务——**包括每日定时（`exec`）自动化任务**——由后台线程分批搬完，
  不会留下指向旧平台的死绑定。只有 OpenAI 认识的历史条目（`web_search_call` 等）
  在同一次改写里剥掉。切换成功后弹窗提醒「Codex 只在启动时读配置」，
  点确认**自动重启 Codex**。
- **`missing field call_id` 从根上治了**：Codex App 自带工具（`codex_app` 命名空间，
  如 `automation_update`）写出的 `function_call_output` **没有 `call_id`** ——
  在会话文件里合法、在 API 请求里必填，所以这段历史一回放服务端就 400。这种孤儿
  藏在**任意**老会话文件里，只扫"最近 N 个"永远找不到。v1.6.6 起**全量清扫**每个
  会话文件（账本增量，之后每轮几乎零成本）：切换时、开界面时、界面常驻时每 60 秒
  各补一轮——因为那些工具还在持续产生新的。判断「文件是不是正在被用」改用 `lsof`
  实测句柄，不再靠猜 mtime。
- **上下文守卫会动手**：切换前体检会话体量；过了建议压缩线会告诉你 Codex 会自动
  压缩、无需操作；真装不下时横幅上直接给「**一键换成装得下的模型**」。
- **能力看得见、测得准**：读图 / 思考 / 工具标注「实测 / 官方 / 推断」，
  实测用两张数方块图（两张都对才算真看得见），结论写回模型目录。
- **平台全量 API 能力，自动配好**：同一把 API Key 不只能对话——生图、生视频、
  语音合成、音乐、联网搜索、向量化。能力页列出全部能力面（带出处）；添加或切换
  平台时自动把**平台官方 MCP Server** 写进 Codex（`[mcp_servers.*]`），并生成
  `0600` 环境变量文件（`~/.codex/model-switcher/env/<id>.sh`），官方 CLI、SDK、
  `curl` 立即可用；删除平台时一并清理。
- **OpenAI 插件全模型可用**：Codex 请求里的 OpenAI 插件原样下发——免费类插件
  （联网搜索）由平台原生执行（MiniMax 实测可用）；需要向 OpenAI 按量计费的插件
  （图片生成等服务端付费工具）需要**额外提供 OpenAI API Key**，费用记在那把钥匙上。
- **余额 / 额度 / 用量**：能查就给真实数字，查不到就明说；本机用量读 Codex 自己的日志。
- **原生 macOS App**：双击即用，后台常驻；Windows / Linux 用浏览器界面，功能一致。
- **中英双语，零依赖**：纯标准库，不需要 pip 装任何东西。

## 支持的平台（预设）

DeepSeek · MiniMax · 智谱 GLM · 月之暗面 Kimi · 阿里云百炼 · 硅基流动 · OpenRouter ·
Groq · Together · Mistral · xAI · Cerebras · Fireworks · DeepInfra · Ollama（本机） ·
LM Studio（本机） · **自定义 / 中转站** —— 任何 OpenAI 兼容接口。

自带 `responses` 接口的平台直连；只有 Chat Completions 的平台走本地协议桥
（`127.0.0.1:8787`，密钥不落盘）。

## 安装

**macOS —— 下载 App（推荐）**

[**⬇︎ 完整版**](https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest/download/Codex-Model-Switcher-macOS-latest-full.zip)
（约 52 MB，自带 Python，什么都不用装）或
[**标准版**](https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest/download/Codex-Model-Switcher-macOS-latest.zip)
（1 MB，需要 Python 3.9+）。两个链接永远指向最新版。
首次打开：右键 →「打开」一次（App 未签名）。

**一条命令（macOS / Linux）**

```bash
curl -fsSL https://raw.githubusercontent.com/yankuanglin1-droid/codex-model-switcher/main/tools/bootstrap.sh | bash
```

**Windows**：clone 后执行 `powershell -ExecutionPolicy Bypass -File install.ps1`
（密钥用 DPAPI 加密）。

**源码**：`bash install.sh`；要 App 再跑 `bash packaging/macos/build_app.sh --with-python`。

**或者让 AI 装**：把 [INSTALL-WITH-AI.md](INSTALL-WITH-AI.md) 整段复制给 Codex / Claude Code。

## 日常使用

```bash
echo "$YOUR_API_KEY" | codex-switcher add --preset deepseek --key-stdin   # 或 --name "我的中转" --base-url https://...
codex-switcher use deepseek          # 切换（最近任务一起搬）
codex-switcher app                   # 图形界面
codex-switcher restore               # 一键切回官方 OpenAI
```

切换后：**完全退出 Codex（⌘Q）再打开**，就这一步。

## 突发情况怎么办

以下都有自动化处理或一条命令的解法，全部先备份再动；详见
[docs/troubleshooting.md](docs/troubleshooting.md)。

| 症状 | 原因 | 解法 |
| --- | --- | --- |
| `unknown model 'xxx'` / `invalid params (2013)` | 任务还绑着上一个平台 | 几秒内自动修复，或 `codex-switcher repair --follow`；重开 Codex |
| `model is not supported ... ChatGPT account` | 在绑着官方 OpenAI 的旧任务里选第三方模型（这类任务刻意不搬） | 对它「分叉」，或新建任务 |
| `missing field call_id`（任何任务、任何平台） | `codex_app` 命名空间工具写出的 `function_call_output` 没有 `call_id`：会话文件里可选、API 里必填 | **v1.6.6 起自动全量清扫**（所有会话文件：切换时 + 界面巡检）；立刻清一次：`codex-switcher history --sweep`（先备份） |
| 一直「压缩上下文」毫无进展 | 会话比目标模型的窗口还大 | `codex-switcher guard`，或点横幅上的「**一键换成装得下的模型**」 |
| `wire_api = "chat" is no longer supported` | 旧的手工配置残留 | `codex-switcher use <平台>` 重写 |
| 502 / 连接被拒 | 协议桥没在跑 | `codex-switcher bridge --install-agent` |
| 其它问题 | — | `codex-switcher doctor`，再 `codex-switcher export`（脱敏）求助 |

## 它是怎么做到的

只改 `~/.codex/config.toml` 的两处：模型指向 + 一个 `[model_providers.<id>]` 块。
配置文件里**没有密钥**，只有一把去系统钥匙串取密钥的助手命令；其余内容（插件、
MCP、项目信任）逐字节保留 —— 写前备份、写后用 TOML 解析复验，越界即整体回滚。

## 安全

- 密钥只进系统钥匙串；没有钥匙串的系统退回 `0600` 本地文件并明确告知。
- 界面 / 日志一律只显示脱敏串。
- 原子写入 + 并发保护；界面与协议桥只监听 `127.0.0.1`，界面带一次性令牌。
- 发布前跑 `tools/scan_secrets.py`（连 git 历史一起扫）。

## 常见问题

**切换后旧对话还能继续吗？**
第三方 ↔ 第三方可以：最近的任务会自动搬过去。绑着官方 OpenAI 的任务刻意不动
（动了会丢账号绑定），要继续就对它「分叉」或新建任务。

**画图 / 联网搜索 / 操作电脑能用吗？**
不能。这些是 OpenAI 服务器侧工具或平台上的独立模型（image-01、video-01…），
Codex 只跟一个对话模型说话。工具不会假装这些能力存在 —— 这正是「能力查看」页的意义。

**本机模型（Ollama / LM Studio）怎么接？**
`codex-switcher add --preset ollama-local`，不需要 Key。注意别把 id 起成 `ollama`
（Codex 保留名）。

更多见 [docs/troubleshooting.md](docs/troubleshooting.md) 与 [docs/app.md](docs/app.md)。

## 开发与验证

```bash
python3 -m unittest discover -s tests      # 138 项测试，无需联网
python3 tools/preflight.py                 # 发布前总自检
python3 tools/verify_codex_catalog.py      # 真实 Codex 能否列出我们的模型
python3 tools/verify_bridge.py             # 真实 Codex 走协议桥拿回答
python3 tools/make_screenshot.py --gif     # 重新生成 README 截图与演示 GIF
```

## 许可证

MIT
