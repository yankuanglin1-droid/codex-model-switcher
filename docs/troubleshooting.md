# 排障手册

> **v1.7.10 历史保护变更：** 启动、默认模型切换和后台巡检不再自动改写原始对话或任务绑定。旧任务与自动化保留原平台，新任务使用新默认平台。旧版本关于自动迁移、自动清扫的说明已失效。参见 [历史保护说明](history-preservation.md)。

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
cat ~/Applications/"ChatGPT Model Switcher.app"/Contents/MacOS/launcher | tail -1
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
input: missing field `call_id` at line 1 column 697172
```

### 这不是平台坏了，是会话历史里有一条坏记录

Codex 会把整段会话历史**原样回放**到下一次请求里。历史里如果有下面这类条目，
服务端解析请求体时就会直接拒绝：

| 条目 | 为什么会被拒绝 |
| --- | --- |
| `function_call_output` 缺 `call_id` | 见下方「真正的病根」。这是**唯一**能凭空产生的一类。 |
| `reasoning` 带 `encrypted_content` | OpenAI 专有的加密推理状态，官方说明它的用途是<br>“在无状态调用之间复用推理”。换到第三方平台后它没有任何意义。 |
| `custom_tool_call` / `web_search_call` 等 | OpenAI 专有类型，第三方实现未必认识（工具只报告，不擅自删）。 |

请求体越大越容易撞上（报错里的 `column N` 就是长对话，几十万字符很正常）。

### 真正的病根：Codex 两套类型定义不对称

Codex 里的会话条目有**两副面孔**，而 `call_id` 在两边的要求不一样：

| 定义 | 位置 | `call_id` |
| --- | --- | --- |
| `ResponseItem` | 写进会话文件（rollout）的那个 | `Option<String>` + `skip_serializing_if = "Option::is_none"` → **可以缺省** |
| `ResponseInputItem` | 发给 API 的请求体里的那个 | `String` → **必填** |

于是：**Codex App 自带的工具**（`codex_app` 命名空间，例如每日自动化用的
`automation_update`）会写出一条只有 `id` / `name` / `namespace` / `output`、
**没有 `call_id`** 的 `function_call_output`。这在本地文件里完全合法，能静静躺着；
一旦这段历史被回放，`call_id` 因为 `skip_serializing_if` 被跳过序列化，服务端
serde 反序列化时找不到必填字段，直接 400。

```
{"type":"function_call_output", "id":"fco_01a0a804-...", "name":"automation_update",
 "namespace":"codex_app", "output":"Automation: ..."}
```

触发场景**不只是继续对话**：Codex 后台生成线程标题/描述的「结构化回合」也会
把整段历史发出去，所以哪怕你只是点开那个旧对话，它也可能报一次。

### 为什么以前的自动修复漏掉了它

三道限制叠在一起，导致存量坏条目**从来没被任何一条路径清过**：

1. `auto_clean` 只扫**最近 30 个**会话文件，`repair` 只扫**最近 10 个**；
2. 所有清理路径都会跳过"最近 120 秒有改动"的文件；
3. 会话文件的清洗是**搭在改绑上的**——任务没改绑，历史就不会被重写。

实测（2026-09-17，本机）：**1347 个会话文件 / 33.9GB，其中 45 个文件里有
72 条**这种孤儿记录，**全部**来自 `codex_app` 命名空间；最老的一条躺在 7 月底的
小文件里 —— 而用户点开哪个就炸哪个。

### 怎么修

**v1.6.6 起，改为"全量清扫 + 持续收敛"**：

- `history.sweep_all()` **不设窗口**，把所有会话文件过一遍；靠账本
  （path → mtime+size）做增量，第一次全量之后每轮几乎零成本。
- 切换平台、打开图形界面时各挂一次后台清扫；图形界面常驻后每 60 秒补扫一轮。
  这样即使 `codex_app` 工具**继续**产生新的孤儿（定时任务每跑一次就可能多一条），
  也会在下一轮被清掉。
- "能不能动这个文件"改由 **lsof 实测**判断 Codex 是否真的攥着它的句柄，
  比猜 mtime 准得多（Codex 会把会话文件句柄常驻，而那个对话可能几小时没动静）。

手动跑一次、随时可查：

```bash
codex-switcher history --sweep --dry-run   # 预演：看看会删什么
codex-switcher history --sweep             # 全量清理（自动备份）
codex-switcher history                     # 只看最近 30 个（只读）
codex-switcher history --clean --cross-provider  # 顺带剥别家服务端工具条目
```

清洗规则刻意保守：**只删“确定是坏的”和“确定对方用不上”的**，其余只报告不动。
孤儿输出是"任何平台都不认"的坏数据（官方也必填 `call_id`），所以默认就清；
别家服务端工具条目只在显式 `--cross-provider` 或"任务真的搬家"时才剥。
留在官方 OpenAI 时不会删推理条目（官方文档要求保留它们）。

注意事项：

- **Codex 正开着的会话会被跳过**（用 lsof 判断句柄）。那多半是你正开着的对话，
  改写它可能把当前对话写坏。关掉它，或在图形界面开着的时候等一轮自动补扫。
- 改之前有备份，放在 `~/.codex/model-switcher/history-backups/`。
- 这个报错只影响**受影响的那个旧对话**；新建任务不会带着这段历史，所以不受影响。

自检时可以一起看：`codex-switcher doctor --history`

---

## 报 `tool type "tool_search" is not supported`（Kimi 等平台）

这两条**是同一个根因**，常常先后出现：

```
invalid_request_error: tools.13: tool type "tool_search" is not supported
invalid_request_error: json: cannot unmarshal object into Go struct field alias.arguments of type string
```

### 病根：Codex 有个内置工具 `tool_search`，它的参数和别人不一样

工具清单太长时（装了插件 / 应用 / 一堆 MCP），Codex 不再把工具逐个列进请求，
而是发一个 `tool_search` 让模型自己搜。问题在于它的 `arguments` 是**对象**，
而普通 `function_call` 的 `arguments` 是**字符串**：

```jsonc
{"type": "function_call",     "arguments": "{\"a\": 1}"}   // 字符串
{"type": "tool_search_call",  "arguments": {"query": "..."}} // 对象
```

严格校验请求体的平台（实测 Kimi / Moonshot）两条都不认：工具类型直接拒，
`arguments` 也按字符串去解析。宽松的平台（如 deepseek）能容忍，
所以常常只有某一家炸，看着像"这个平台有问题"。

实测本机会话文件：`function_call.arguments` 2567 条全是字符串，
`tool_search_call.arguments` 14 条全是对象。

> 排查时注意：`tools[]` 清单**不落会话文件**，搜会话里的 `"type": "tool_search"`
> 永远是 0 条。要么按上面看条目类型的 `arguments`，要么抓包。

### 三处一起处理（v1.6.8 起自动）

1. **模型目录**：第三方平台的 `include_apps_usage_instructions` 与
   `supports_search_tool` 置为 `false`，Codex 就不会注册这个工具。
   （skills / plugins 仍然开着，本地能力不受影响。）
2. **按平台的硬开关**：切到不兼容的平台时写 `[features] tool_search = false`，
   切回官方或别的平台再打开。单个平台可以用 `supports_tool_search` 覆盖。
3. **清历史残留**：`tool_search_call` / `tool_search_output` **成对剥离**。
   这一步不做的话，老会话回放照样报错 —— 那些条目已经写进文件了。

切换时自动做（先备份）；想立刻手动清一次：

```bash
codex-switcher history --sweep --cross-provider
```

> **改完源码不等于生效**：模型目录是生成物，要重新生成才落盘；`.app` 自带一份
> 运行时代码，要重建替换界面才跑新版本。核对办法看下面一节。

---

## Codex 取不到密钥：`provider auth command ... exited with status signal: 6 (SIGABRT)`

```
provider auth command `/Users/<你>/.codex/bin/codex-provider-keychain.py`
exited with status signal: 6 (SIGABRT)
dyld: Library not loaded: /System/Library/Frameworks/CoreFoundation.framework/...
Referenced from: .../Python.framework/Versions/3.7/.../Python
```

**表现**：图形界面能正常打开、切换也显示成功，但 Codex 一发请求就说取不到密钥，
所有第三方模型都用不了。一半正常一半坏，特别容易误判成"平台没配对"。

### 原因：凭据助手被绑到了一个启动不了的 Python 上

`~/.codex/bin/codex-provider-keychain.py` 是本工具生成的、Codex 每次请求都要
执行的取密钥脚本。它以前的第一行是：

```python
#!/usr/bin/env python3
```

`env` 的意思是"按 PATH 去找 python3"。而 **Codex 拉起它时的 PATH 不由我们控制**。
如果 PATH 里第一个 `python3` 是个很老的版本（常见于很早装过 python.org 安装包的
机器，比如 3.7），它在新版 macOS 上连 `CoreFoundation` 都加载不了，
进程直接 SIGABRT —— **连 Python 都没起来**，所以报错里是 dyld 的信息，
看不到任何 Python 报错。

对比：图形界面没事，是因为 `launch.sh` 启动时会**探测**解释器，坏的那个被跳过了。

### 解法

**临时（不用升级）** —— 重跑一次初始化，或直接改那一行：

```bash
codex-switcher init                    # 升级到 v1.6.9 之后，这一条就够
```

手改也行，把脚本第一行换成一个确实能用的解释器：

```bash
# 先确认哪个能用（能打印出版本号、且 ≥ 3.9）
/usr/bin/python3 --version
# 换成它
sed -i '' '1s|.*|#!/usr/bin/python3|' ~/.codex/bin/codex-provider-keychain.py
```

**根治（v1.6.9 起）** —— 生成脚本时不再把解释器交给 PATH，而是当场**逐个真跑一遍**，
把第一个能执行的、版本 ≥ 3.9 的解释器**绝对路径**写进 shebang。

判断标准是"能不能真的执行"，不是"版本号看起来够不够"：那个 3.7 的版本号也读得到，
但它根本启动不了，所以只看版本号是拦不住的。

> 顺带一提：这就是为什么**推荐 Intel Mac 用完整版** —— 完整版自带 Python，
> 不依赖机器上那个不知道还能不能用的 `python3`。

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

### Returning to OpenAI: `invalid_id_prefix` / expected `msg`

Some native Responses providers emit message IDs that OpenAI rejects when a
conversation is resumed. The switcher now normalizes non-OpenAI message IDs in
ordinary response records and compacted history. Message text, tool calls and
`call_id` associations are preserved. Already valid message IDs are unchanged.

History files held open by ChatGPT/Codex must be closed before on-disk repair.
The repair keeps a private backup and checks the written bytes. Restart the
host after repair to discard its cached copy of the old history. This addresses
message-ID validation; it does not promise compatibility for every provider's
reasoning state or server-side tools.

切回官方后若遇到 `invalid_id_prefix`：新版会规范普通历史与压缩历史中的消息
ID，保留正文、工具调用及配对关系。正在被 ChatGPT/Codex 打开的历史不能直接
覆盖；须先关闭宿主应用，备份修复后重新打开，避免旧缓存再次提交错误 ID。

### In-app repair (v1.7.7)

Use **Repair official history & restart** in the app footer. The app gracefully
quits ChatGPT/Codex, backs up affected histories, normalizes message IDs, and
reopens the host. Progress, skipped files and failures remain visible. Do not
reopen the host while repair is running. A cancelled quit never force-kills the
host or edits history. The restart action uses the same recovery path.

App 内点击底部 **修复官方历史并重启**，即可完成退出、备份、消息 ID 修复和
重新打开，无须下载额外脚本。修复过程中请不要手动打开宿主。界面显示检查、
修复、跳过及失败数量。若退出被取消，不会强制终止进程。

The automation banner is a read-only audit. Fixed-model schedules and
thread-bound heartbeats are different: changing the default provider alone
cannot validate or update both. Review flagged jobs in the host automation
settings. No schedule, prompt, account credential or trigger time is changed
by the audit; successful scheduled execution still needs runtime verification.

### v1.7.8: false missing `[model_providers.openai]` warning

`openai` is the built-in provider. Restoring the official configuration intentionally
omits a custom provider table. The previous readiness check incorrectly required
that table for every provider and displayed a red failure. The corrected check
accepts the built-in provider while keeping third-party table checks. This local
check does not prove authentication, quota, or network availability.

官方平台缺少自定义配置段的红色提示是旧版检测误报，不代表订阅已失效。
黄色自动化检查是独立的只读诊断，也不代表那些计划已经执行失败。
