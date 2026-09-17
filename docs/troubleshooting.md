# 排障手册

先做这两件事，90% 的问题都能定位：

```bash
codex-switcher doctor     # 环境自检
codex-switcher export     # 输出脱敏状态（不含密钥），可以贴出来求助
```

---

## 双击 App 没反应 / 报 Operation not permitted

**原因**：macOS 会保护「文稿 / 桌面 / 下载」这几个目录。如果你的仓库克隆在
`~/Documents` 下，从访达启动的 App **没有权限执行**里面的脚本，系统直接返回
`Operation not permitted`，看起来就是「双击没反应」。

**解决**：重新跑一次安装脚本，它会把运行时代码复制到非受保护目录，App 指向那里。

```bash
bash install.sh
bash packaging/macos/build_app.sh
```

之后仓库放在哪里都不影响使用。想确认 App 到底指向哪：

```bash
cat ~/Applications/"codex（ChatGPT App）多平台模型切换.app"/Contents/MacOS/launcher | tail -1
cat ~/.codex/model-switcher/launch.log     # 启动日志，失败原因会写在这里
```

---

## 界面打开很慢 / 一直转圈

早期版本会整份扫描 Codex 的会话日志来统计用量，机器上会话一多就会卡到超时。
1.1.0 起改成只读文件开头（判断平台）和结尾（取最后一次 token 统计），
1360 个会话也只花 0.3 秒。

如果你装的还是旧版：

```bash
codex-switcher update --pull && bash install.sh
```

---

## 怎么知道是不是最新版

```bash
codex-switcher update        # 只检查
codex-switcher status        # 最后一行也会带版本状态
```

界面底部同样有「检查更新」按钮。

---

## Windows 相关

**装完命令找不到？**
`install.ps1` 把启动器目录加进了「用户 PATH」，但已经开着的终端不会自动刷新。
**新开一个** PowerShell / CMD 再试。

**图形界面怎么开？**
双击 `%LOCALAPPDATA%\codex-switcher\bin\codex-switcher-gui.vbs`，
它会后台拉起服务并打开浏览器（不弹黑框）。也可以直接跑 `codex-switcher app`。

**密钥存哪了？**
`%LOCALAPPDATA%\codex-switcher\credentials\<平台>.key`，内容是 **Windows DPAPI
加密**过的，只有当前 Windows 用户能解开。换用户或换电脑都读不出来。

**提示 `No module named codex_switcher`？**
说明启动器里的 `PYTHONPATH` 没生效。检查
`%LOCALAPPDATA%\codex-switcher\bin\codex-switcher.cmd` 里的路径，重新跑一次
`install.ps1` 也能修好。

**界面里点按钮没反应？**
浏览器可能拦截了弹窗（`confirm` / `prompt`）。在地址栏右侧允许本站弹窗即可。
原生窗口的 macOS App 不存在这个问题。

---

## 切换后模型列表没有变化

**症状**：切到 DeepSeek 了，但 Codex 里还是原来那批模型。

按顺序排查：

1. **有没有完全退出 Codex**。必须 ⌘Q 退出再打开，只关窗口不生效。
2. **是不是旧任务**。服务商是按任务绑定的，旧任务不会跟着切换。新建一个任务看。
3. **provider id 是否撞了保留名**。`ollama`、`lmstudio`、`openai`、`codex`、`azure`
   都是 Codex 自己占用的名字，用它们会导致 Codex 忽略你的模型目录。
   执行 `codex-switcher list` 看 id，必要时删掉重建。
4. **模型目录是否存在且内容正确**：
   ```bash
   codex-switcher models <平台ID>
   ```
   如果这里是空的，先 `codex-switcher refresh <平台ID>`。

---

## Codex 启动报 `wire_api = "chat" is no longer supported`

配置文件里有旧的手工配置。工具自己永远只写 `responses`，所以这通常意味着
你在加入本工具之前手工配过，或者被别的脚本写过。

```bash
codex-switcher use <平台ID>     # 重写一遍，工具只会写 responses
```

如果重写后仍然报错，说明还有另一个 provider 块留着 `chat`：

```bash
grep -n 'wire_api' ~/.codex/config.toml
```

把 `"chat"` 全部换成 `"responses"`；只支持 Chat 的平台请改用协议桥。

---

## 报 `The 'xxx' model is not supported when using Codex with a ChatGPT account`

你正在一个**绑定 OpenAI 的旧任务**里选第三方模型。这个任务在创建时就把服务商
写死成 OpenAI 了，之后改默认服务商不会回头改它。

两个办法：

1. 对该任务使用「分叉（Fork）」，在分叉出来的新任务里选择目标模型。
2. 在同一目录新建一个任务。

想批量看看哪些任务还绑在旧服务商：

```bash
codex-switcher list        # 至少能确认当前默认是哪个平台
```

---

## 继续旧任务报 `unknown model 'xxx'` / `invalid params, code: 2013`

```text
invalid params, code: 2013, msg: invalid params, unknown model 'deepseek-flash' (2013)
```

### 这是切换平台后最常见的事故，原因一句话就能说清

Codex 恢复一个旧任务时，**服务商取任务自己记的那个**（写在会话文件的
`session_meta.model_provider` 和每轮的 `thread_settings.model_provider_id` 里），
**模型名却取当前配置里的**。两者一分家，请求就带着新平台的模型名敲进旧平台的
接口 —— 上面那条 2013 就是把 deepseek 的模型名发给 MiniMax 时，MiniMax 的原话。

绑定时写在**三处**：`state_5.sqlite`、`sqlite/codex-dev.db`、会话文件本身。
Codex 自己会把数据库纠正过来，但**会话文件不会**，所以只看数据库「一切正常」、
一继续任务照样报错。

### 自动化处理（v1.6.0 起，通常你什么都不用做）

1. **切换时就搬**：`use` 切换平台时，最近 36 小时还在用的任务会整体搬到新平台
   （会话文件 + 两个数据库 + 模型名一起改，先备份到
   `~/.codex/model-switcher/thread-backups/`）。
2. **后台巡检**：图形界面服务每 12 秒复查一次，发现错位直接修好。切完继续任务
   万一还报错，**等几秒重试一次**即可。
3. OpenAI 的任务刻意不搬（那是「老家」，且一动就是上千条）。

### 手动处理

```bash
codex-switcher tasks           # 看哪些任务的服务商和模型对不上
codex-switcher repair          # 修复（默认含最近任务的会话文件扫描）
codex-switcher repair --follow # 把最近在用的任务整体搬到当前平台
codex-switcher repair --deep   # 全量扫描会话文件（慢，会话文件可能几百 MB）
```

修完**完全退出（⌘Q）并重开 Codex**。正在被 Codex 写入的会话文件会跳过
（避免把正在进行的对话写坏），退出后再跑一次即可。

---

## 请求报连接失败 / 502 / 连接被拒绝

分两种情况。

### 走协议桥的平台

```bash
codex-switcher doctor      # 会明确说协议桥有没有在跑
codex-switcher bridge      # 先手工跑起来验证
codex-switcher bridge --install-agent   # 确认没问题后装成后台服务
```

注意：桥没在跑的时候，Codex 会报连接 `127.0.0.1:8787` 失败，这是预期行为。

### 直连的原生平台

先确认本机能连上平台：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.deepseek.com/models
```

返回 `401` 是正常的（说明接口在，只是没带密钥）。如果返回 `000` 或域名被解析到
`127.0.0.1`，说明本机的代理 / hosts / DNS 拦截了它——这跟平台无关，先修网络。

---

## API Key 相关

**怎么确认密钥存进去了？**

```bash
codex-switcher list        # 每行末尾会显示脱敏后的密钥
```

**密钥存哪了？**

- macOS：登录钥匙串，服务名 `com.codex.model-switcher.<平台ID>`
- Linux：Secret Service（`secret-tool`）
- 都没有：`~/.codex/model-switcher/credentials/<平台ID>.key`，权限 0600

**想换密钥？** 直接重新 `codex-switcher add` 同一个平台，会覆盖旧密钥
（平台 id 相同即覆盖）。

**想彻底删掉？** `codex-switcher remove <平台ID>`，会同时删掉钥匙串条目。

---

## 余额显示"未开放接口"

这是如实反馈，不是 bug。MiniMax、智谱 GLM 等套餐制平台没有公开的额度查询接口，
工具不会编数字，只给官网入口。

如果你的平台有余额接口，可以在添加时告诉工具：

```bash
codex-switcher add --name X --base-url https://api.example.com/v1 \
  --balance-url https://api.example.com/me/balance \
  --balance-path data.balance --balance-currency CNY --key-stdin
```

`--balance-path` 用点号表示层级，数组下标直接写数字，例如
`balance_infos.0.total_balance`。

---

## 报 `missing field call_id`（或第三方平台拒绝整个请求）

```
Failed to deserialize the JSON body into the target type:
input: missing field `call_id` at line 1 column 614675
```

### 这不是平台坏了，是会话历史里有一条坏记录

Codex 会把整段会话历史**原样回放**到下一次请求里。历史里如果有下面这类条目，
服务端解析请求体时就会直接拒绝：

| 条目 | 为什么会被拒绝 |
| --- | --- |
| `function_call_output` 缺 `call_id` | 官方文档把它列为迁移常见错误：<br>“Sending a function result without the matching `call_id`”。<br>服务端只能 400。实测机器上 1089 条里有 15 条是这种孤儿记录，<br>来源是 Codex App 自带的工具（`codex_app` 命名空间）——只写了输出没写调用。 |
| `reasoning` 带 `encrypted_content` | OpenAI 专有的加密推理状态，官方说明它的用途是<br>“在无状态调用之间复用推理”。换到第三方平台后它没有任何意义。 |
| `custom_tool_call` / `web_search_call` 等 | OpenAI 专有类型，第三方实现未必认识（工具只报告，不擅自删）。 |

请求体越大越容易撞上（上面报错里的 `column 614675` 就是长对话）。

### 官方有没有开关让它容忍？没有

- **OpenAI 官方**：把它当**客户端错误**，没有服务端开关。官方给的规避方式是
  **服务端会话状态**（`previous_response_id` / `conversation` / `store`）——
  客户端不用重放原始 items，自然也就不会把坏条目送出去。
- **第三方平台官方**：同样没有“容忍畸形输入”的选项。而它们普遍**不支持**
  `previous_response_id`，只能靠客户端重放历史，所以这条路走不通。

结论：这个只能在**本地把历史清干净**。

### 怎么修

**v1.6.3 起，绝大多数情况已经自动修好**：任务从一个平台改绑到另一个平台时
（切换跟随 / `repair` / 深度修复），会话历史里的跨平台条目和孤儿输出会在
**同一次改写里剥掉**——不存在「只改绑、不清洗」的路径。ChatGPT 的老任务
（含每日定时任务）会在切换后的后台迁移中一并处理。

存量文件（机制上线前就已经搬过的）用命令手动清：

```bash
codex-switcher history                     # 检查最近 30 个会话（只读）
codex-switcher history --clean --dry-run   # 预演，看看会删什么
codex-switcher history --clean --cross-provider  # 真的清（自动备份）
```

清洗规则刻意保守：**只删“确定是坏的”和“确定对方用不上”的**，
其余只报告不动。留在官方 OpenAI 时不会删推理条目（官方文档要求保留它们）。

注意事项：

- **正在写入的会话会被跳过**（最近 120 秒有改动）。那多半是你正开着的对话，
  改写它可能把当前对话写坏。完全退出 Codex 后再跑一次即可。
- 改之前有备份，放在 `~/.codex/model-switcher/history-backups/`。
- 这个报错只影响**受影响的那个旧对话**；新建任务不会带着这段历史，所以不受影响。

自检时可以一起看：`codex-switcher doctor --history`

---

## 一直在「压缩上下文」，任务却毫无进展

### 症状

切到第三方模型之后，对话里不停出现压缩/交接摘要，十几秒一次，
模型不做正事，token 一直在烧。

### 原因：压缩不收敛，而不是历史太长

第三方模型在模型目录里声明的上下文窗口通常远小于官方模型。
`deepseek-flash` 声明 131072，Codex 按 `effective_context_window_percent = 95`
算出可用窗口：

```
131072 × 0.95 = 124518
```

一个在官方模型上已经跑到 36 万 token 的会话切过去之后：

1. 每轮请求体（约 36 万）> 可用窗口（124518），Codex 判定超出预算 → 压缩；
2. 压缩后主对话确实降到 3 万左右，**但下一轮请求又回到 36 万**；
3. 于是再压缩，如此循环。

第 2 步是关键。看一次压缩事件的实际构成就明白了：

```
message（摘要）            5.7 KB
replacement_history       41.8 KB   ← 真正留给下一轮的历史，很小
guardian_history         902.9 KB   ← 压缩顺带存档的完整快照
```

**压缩产物本身就比目标窗口还大**：那条 900KB 的 `guardian_history` 会被下一轮
一起回放，体量重新回到 36 万。这是个自我喂养的死循环。
（顺带一提，它也是会话文件能膨胀到 269MB 的原因：199 × 1MB。）

实测数据（2026-09-17，会话 `01a0a8cd`）：17 分钟内触发 **199 次**压缩，
平均每 15~20 秒一次。

### 怎么判断

```bash
codex-switcher guard                      # 体检最近那个会话
codex-switcher guard --model deepseek-flash --thread <会话文件路径>
```

输出示例：

```
会话体量：364,713 tokens
目标模型可用窗口：124,518 tokens
切换时会写入的压缩触发点：74,710 tokens
占用：293%
建议：装不下，必须分叉或新开任务，不要原地续接
```

`codex-switcher use` 切换时也会自动体检，装不下会当场提示。

### 怎么办

- 占用 **> 100%**：新开一个任务，或对这个会话「分叉」后再换模型。不要原地续接。
- 占用 **60%~100%**：续接前先在对话里手动压缩一次。
- 占用 **< 60%**：正常，直接继续。

### 工具已经做了什么

切到第三方模型时，工具会把 `model_auto_compact_token_limit` 一起写进
`config.toml`，取可用窗口的 **60%**。在窗口的 60% 处触发压缩，
压完的历史才有地方落脚 —— 压缩必须是收敛的，否则压多少次都没用。

---

## Python 版本问题

报 `当前 Python 没有 TOML 解析库`：

- 升级到 Python 3.11+（推荐），或
- `python3 -m pip install tomli`

`install.sh` 会自动在 `python3.14 / 3.13 / 3.12 / 3.11` 里挑一个合适的。

### 找不到 Python（macOS）

报 `找不到 Python 3.9 或更高版本` 时，按顺序看这几处（脚本也是按这个顺序找的）：

1. App 自带的 `runtime/python-<架构>/bin/python3`（只有完整版才有）
2. `~/.codex/model-switcher/python-path`（安装时记下的）
3. `$CODEX_SWITCHER_PYTHON`
4. Homebrew：`/opt/homebrew/bin/python3.x`、`/usr/local/bin/python3.x`
5. python.org 官方安装：`/Library/Frameworks/Python.framework/Versions/3.x/bin/python3`
6. 系统自带：`/usr/bin/python3`（装了 Xcode 命令行工具才有，通常是 3.9.6）

都没有的话，任选一条免费路子：

```bash
xcode-select --install          # 装 Apple 的命令行工具，自带 Python 3.9.6
# 或者到 https://www.python.org/downloads/macos/ 下安装包双击装
```

### 找不到 Python（Windows）

装的时候记得勾 **Add python.exe to PATH**。装完可以验证：

```powershell
py -3 --version
```

如果用户名是中文（`C:\Users\张三\…`），安装脚本会用 ANSI 代码页写启动器，
这样命令行才认得这些路径 —— 旧版本用 ASCII 写，会把路径写成 `?` 导致永远找不到
Python，这个问题已经修掉。

---

## 想回到一切都出问题之前

每次改动前都会备份：

```bash
ls -lt ~/.codex/model-switcher/backups/ | head
```

挑一个时间点，复制回 `~/.codex/config.toml` 即可。备份是改前的原文，逐字节一致。
