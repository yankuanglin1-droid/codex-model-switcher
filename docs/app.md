# App 使用说明

## 它是什么

一个原生 macOS 窗口应用：**ChatGPT Model Switcher**。

图标和界面用的是同一枚标 —— Codex 的云朵形状加终端提示符，黑白配色、白色为主、
液态玻璃质感。矢量源文件在 `packaging/macos/icon/appicon.svg`，跑
`python3 packaging/macos/icon/make_icon.py` 可以重新生成图标和界面 logo。

## 装上之后能得到什么

| 你能做的事 | 点哪里 |
| --- | --- |
| 看到当前用的是哪个平台、哪个模型 | 打开就显示在标题下方 |
| 把任意平台的模型接进来 | 右上角「手动添加平台」 |
| 在平台的全部模型之间切换 | 左栏选平台 → 模型列表点「切换」 |
| 看真实余额 / 额度 | 选中平台后自动查询，能查的显示真数字 |
| 看本机用量和套餐占比 | 「本机用量」卡片；没填额度就只显示已用量 |
| 检查是不是最新版 | 底部「检查更新」 |
| 回到官方 OpenAI | 右上角「恢复官方 OpenAI」 |
| 停掉后台服务 | 菜单「服务 → 停止后台服务」，或 `codex-switcher stop` |

## 双击之后发生了什么

1. 检查是不是已经有界面在跑（读 `~/.codex/model-switcher/gui.json`），有就直接装进窗口
2. 没有就拉起两个后台进程：协议桥（8787 端口）和界面服务（随机端口，只监听本机）
3. 用原生窗口（WKWebView）把界面装进来，**不开浏览器、不弹终端**

窗口标题栏、Dock、菜单栏都是它自己的。退出窗口不会停掉后台服务——协议桥还要给
Codex 转发请求；要停就用菜单里的「停止后台服务」。

## 最重要的功能：切换模型不丢上下文

### 问题长什么样

在 Codex 里换模型，有时会突然报：

```text
The 'deepseek-flash' model is not supported when using Codex with a ChatGPT account.
```

而且感觉像是「这个对话废了」——换过去用不了，换回来又不确定有没有丢东西。

### 为什么会这样

Codex 把「这个对话属于哪个服务商」**写死在创建的时候**，存在三处：

1. `state_5.sqlite` 的 `threads.model_provider`
2. `sqlite/codex-dev.db` 的 `local_thread_catalog.model_provider`
3. 会话文件里的 `session_meta.payload.model_provider`，以及每一轮的
   `event_msg.payload.thread_settings.model_provider_id`

切换默认服务商**不会**回头改旧对话。于是你在一个还绑着 OpenAI 的旧对话里选了
DeepSeek 的模型，请求就会带着 `deepseek-flash` 这个名字发到 ChatGPT 账号，被拒。

### 怎么修

原则只有一条：**对话用的模型属于哪个平台，这个对话的服务商就应该是哪个平台。**

App 会：

- **打开就检查**：发现对不上的对话，顶部弹一条提示，点「一键修复」
- **切换时顺手修**：每次切平台，自动把对不上的对话一起对齐
- **后台每 5 秒巡检**：协议桥里跑着巡检线程，发现新出现的错配立刻修（可写进
  `~/.codex/model-switcher/threads.log`）

修复会改上面三处，并且：

- 改前把数据库和会话文件都备份到
  `~/.codex/model-switcher/thread-backups/<时间戳>/`
- 只动服务商字段，其余内容逐字节保留，改完仍是合法 JSONL
- 跳过最近还在写入的会话文件（避免和 Codex 抢同一个文件），下次再修

命令行同样可用：

```bash
codex-switcher tasks                    # 看有没有对不上的
codex-switcher repair --dry-run         # 预演
codex-switcher repair                   # 修
codex-switcher repair --deep            # 顺带清理会话文件里的历史残留（约 30 秒）
```

修完**完全退出并重新打开 Codex**，旧对话就能用新模型接着聊，上下文都在。

### 为什么不会丢上下文

对话内容存在会话文件里，我们**只改「服务商」这个字段**，不动任何消息内容。
改的是「这个对话该找谁说话」，不是「这个对话说过什么」。

## 常见问题

**必须重启 Codex 吗？**
要。Codex 在启动时读这些绑定，改完不重启不生效。

**修复会改我的对话内容吗？**
不会。只改服务商字段，其余逐字节保留，而且改前有备份。

**为什么有的对话被跳过？**
它最近两分钟还在写入，说明可能正开着。等一会儿再点一次修复即可。

**App 需要什么前置条件？**
macOS 11+（Apple Silicon / Intel 都行）。

- **完整版**（下载文件名带 `-full`）：什么都不用装，自带 Python，解压双击即可。
- **标准版**：机器上需要一个 Python 3.9+。没有的话 App 会弹窗给出两条免费做法——
  终端执行 `xcode-select --install`，或到 python.org 下载安装包。

App 本体是 arm64 + x86_64 通用二进制，两种芯片都能跑。

**第一次打开被系统拦下？**
App 没有 Apple 开发者签名。右键 →「打开」，点一次之后就不再问了。
