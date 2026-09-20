# 让 AI 帮你装（一键指令）/ Let an AI install it for you

不用自己敲命令。把下面整段复制给 **Codex / Claude Code / Cursor / 任何能执行命令的 AI**，
它会自己问你要缺少的信息，然后装好、配好、验证完，最后告诉你按哪个键切换。

Just paste the whole block into **Codex / Claude Code / Cursor / any agent that can run shell
commands**. It will ask for what it is missing, do the install, verify, and tell you how to switch.

---

## 中文指令（复制这一段）

```text
请帮我在本机装好并配置「ChatGPT Model Switcher」
（仓库：https://github.com/yankuanglin1-droid/codex-model-switcher）。

按顺序做，不要跳步，每一步把结果贴给我：

【第一步：看清环境】
0. 仓库里有一份给 AI 看的详细执行手册 `docs/ai-setup.md`，先把它读完再动手，
   里面有保留名、协议桥、证据要求等硬性约束。
1. 告诉我当前是什么系统（macOS / Windows / Linux）、macOS 的话芯片是 Apple 还是 Intel、
   以及 python3 --version 的结果。缺 Python 就先用免费办法装好（macOS 可跑
   xcode-select --install，Windows 到 python.org 下载并勾选 Add python.exe to PATH）。

【第二步：安装】
2. macOS / Linux：
     git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
     cd codex-model-switcher && bash install.sh
   Windows（PowerShell）：
     git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
     cd codex-model-switcher
     powershell -ExecutionPolicy Bypass -File install.ps1
3. 装完执行 codex-switcher doctor，把输出贴给我。有问题先修好再往下走。

【第三步：问我要信息（缺一不可）】
4. 问我：
   - 平台名称（例如 DeepSeek / MiniMax / 智谱 GLM / OpenRouter / 我自己的中转站）
   - Base URL（如果我不知道，你去该平台官方文档查准了再填，不要猜）
   - API Key
   - 是否只想要某几个模型（不说就自动拉全部）
   如果平台在内置预设里（先跑 codex-switcher presets 看），就用 --preset；
   不在就用自定义方式填 Base URL。

【第四步：写进去】
5. 用我给的 Key 执行下面这种命令，密钥一律从标准输入传：
     codex-switcher add --preset <预设ID> --key-stdin      # 内置平台
     codex-switcher add --name "<平台名>" --base-url "<地址>" --key-stdin   # 自定义
   绝对不要：把 Key 写进命令行参数（会进命令历史）、写进任何文件、
   写进 config.toml、或者在对话里回显出来。

【第五步：切换并验证】
6. codex-switcher use <平台ID>，然后 codex-switcher doctor 复验一遍。
7. 如果 doctor 说需要协议桥，执行 codex-switcher bridge --install-agent。

【第六步：告诉我三件事】
8. 告诉我：
   - 现在 Codex 用的是哪个平台、哪个模型
   - 怎么切回官方 OpenAI（给出确切命令）
   - 切换后必须完全退出并重新打开 Codex 才生效
9. 还要提前提醒我一句：如果切换模型后某个旧对话报
   “model is not supported when using Codex with a ChatGPT account”，
   那是旧对话还绑着原来的服务商，执行 codex-switcher repair 可以就地修好。
10. 最后问我一句：要不要顺手装个能双击打开的图形界面
    （macOS: bash packaging/macos/build_app.sh --with-python）。
```

---

---

## 只要图形界面 / Intel Mac / 不想碰命令行（复制这一段）

适用于：**Intel Mac**、机器上没装 Python、只想双击打开就能用的场景。
发行包里有两个版本，**Intel Mac 一律选「完整版」** —— 它自带 x86_64 的 Python，
机器上什么都不用装；标准版依赖系统已有的 Python 3.9+。

```text
请帮我在这台 Mac 上装好并配置最新版「ChatGPT Model Switcher」
（仓库：https://github.com/yankuanglin1-droid/codex-model-switcher）。

按顺序做，不要跳步，每一步把实际输出贴给我。

【第一步：看清这台机器】
1. 先执行 uname -m 并贴出结果。
   - 结果是 x86_64（Intel）→ 必须下载**完整版**（自带 Intel 架构的 Python）
   - 结果是 arm64（Apple 芯片）→ 完整版、标准版都行；机器上已有 Python 3.9+
     （python3 --version 确认）可以用标准版，体积小很多
2. 顺便确认 macOS 版本（sw_vers），需要 11.0 以上。
3. 确认 /Applications 或 ~/Applications 可写、当前用户有管理员权限。

【第二步：下载并校验（不要跳过校验）】
4. 下载（这是永久链接，永远指向最新版，不要自己拼版本号）：
   完整版（约 52 MB，自带 Python，推荐 Intel Mac 用）：
     https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest/download/Codex-Model-Switcher-macOS-latest-full.zip
   标准版（约 1 MB，需要本机已有 Python 3.9+）：
     https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest/download/Codex-Model-Switcher-macOS-latest.zip
   下载不通就换办法（gh release download、或换网络环境），但**不要**改成从
   第三方镜像拿，也**不要**改成 clone 仓库自己打包。
5. 校验，三项都要做，把结果贴给我：
   a. 解压后确认 App 主程序是通用二进制：
        file "<解压目录>/ChatGPT Model Switcher.app/Contents/MacOS/CodexSwitcherApp"
      期望看到 "universal binary with 2 architectures: [x86_64 ...] [arm64 ...]"
   b. 如果下的是完整版，确认自带了**本机架构**的 Python：
        file "<解压目录>/ChatGPT Model Switcher.app/Contents/Resources/runtime/python-$(uname -m)/bin/python3"
      Intel 机上期望看到 "Mach-O 64-bit executable x86_64"。看不到就说明包不对，换完整版。
   c. 确认版本：
        grep __version__ "<解压目录>/ChatGPT Model Switcher.app/Contents/Resources/runtime/codex_switcher/__init__.py"
   d. 确认签名没坏：
        codesign --verify --deep "<解压目录>/ChatGPT Model Switcher.app"
      没有输出就是通过了；报 "code object is not signed at all" 说明包坏了，重新下载。

【第三步：安装】
6. 把 .app 拖进「应用程序」（/Applications）。命令行等价做法：
     ditto "<解压目录>/ChatGPT Model Switcher.app" "/Applications/ChatGPT Model Switcher.app"
   如果机器上已经装过旧版，先备份再替换：
     mv "/Applications/ChatGPT Model Switcher.app" ~/Desktop/switcher-old.app
7. 双击打开。如果系统弹「无法打开，因为它来自身份不明的开发者」或「文件已损坏」，
   **不要**删掉重来：去「系统设置 → 隐私与安全性」，往下滑到刚被拦的那条，点「仍要打开」。
   这一步是 macOS 的隔离机制，不是包坏了。

【第四步：问我要信息（缺一不可，不要替我猜）】
8. 问我：
   - 平台名称（例如 DeepSeek / MiniMax / 智谱 GLM / Kimi / OpenRouter / 我自己的中转站）
   - Base URL（我不知道的话，你去该平台官方文档查准了再填，**绝对不要凭印象编**）
   - API Key
   - 只要某几个模型，还是自动拉全部（不指定就拉全部）
   如果平台在内置预设里（codex-switcher presets 可以看），用预设；不在就自定义 Base URL。

【第五步：写进去（密钥安全是硬要求）】
9. 密钥一律走标准输入，或者由我自己在界面里粘贴：
     printf '%s' "$KEY" | codex-switcher add --preset <预设ID> --key-stdin
     printf '%s' "$KEY" | codex-switcher add --name "<平台名>" --base-url "<地址>" --key-stdin
   图形界面里有输入框的，就让我自己贴，不要你在命令里代填。
   绝对不要：把 Key 写进命令行参数（会进 shell 历史）、写进任何文件、
   写进 config.toml、粘贴回对话、或者写进提交。
10. 如果这台上只有图形界面、没有 codex-switcher 命令，就先装命令行：
      git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
      cd codex-model-switcher && bash install.sh
    然后再回来做第 9 步。

【第六步：切换并拿证据】
11. 切过去：codex-switcher use <平台ID>（或在界面里点选）。
12. 拿证据，四项都要贴给我，缺一项就说明还没成：
      codex-switcher status      # 显示的平台/模型是我要的那套
      codex-switcher models <平台ID>   # 能列出模型
      codex-switcher doctor      # 没有报错
      codex-switcher list        # 平台确实写进去了
   如果你 doctor 说需要协议桥，执行 codex-switcher bridge --install-agent。
13. **不要替我宣布成功**：最后一项要我重启 Codex 后自己确认模型列表里有目标模型。

【第七步：告诉我四件事】
14. 告诉我：
   - 现在 Codex 用的是哪个平台、哪个模型
   - 怎么切回官方 OpenAI（给出确切命令）
   - 切换后必须**完全退出（⌘Q）再重开** Codex 才生效，只关窗口不算
   - 如果某个旧对话报 "model is not supported when using Codex with a ChatGPT account"，
     那是旧对话还绑着原来的服务商，不是坏了：执行 codex-switcher repair，或对它分叉
15. 额外提醒我一句：升级后**要切一次平台（或 codex-switcher refresh）**，
    模型目录才会重新生成 —— v1.6.8 修的 Kimi tool_search 报错就靠这一步落地。
    只装新版不切平台的话，能力开关还是旧的。
```

> Intel Mac 上如果只想用界面、完全不碰终端：做完第一到第三步 + 第四步问到信息后，
> 可以直接在界面里填 Key、选平台、切换，第五到第七步里带 `codex-switcher` 的命令
> 可以跳过。但这样就拿不到 `doctor` 的自检证据，出问题不好定位。

---

## English prompt (copy this one)

```text
Please install and configure "Codex (ChatGPT App) Multi-Provider Model Switcher" on this machine.
Repo: https://github.com/yankuanglin1-droid/codex-model-switcher

Do it in order, don't skip steps, and show me the output of each step.

Step 1 - Environment
0. The repo contains a detailed execution manual for agents at `docs/ai-setup.md`.
   Read it first - it lists the hard constraints (reserved provider ids, the local
   bridge, what counts as evidence).
1. Tell me the OS (macOS / Windows / Linux), the chip on macOS (Apple silicon or Intel),
   and the output of `python3 --version`. If Python is missing, install it the free way
   first (macOS: `xcode-select --install`; Windows: python.org installer with
   "Add python.exe to PATH" checked).

Step 2 - Install
2. macOS / Linux:
     git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
     cd codex-model-switcher && bash install.sh
   Windows (PowerShell):
     git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
     cd codex-model-switcher
     powershell -ExecutionPolicy Bypass -File install.ps1
3. Run `codex-switcher doctor` and show me the output. Fix anything it flags before continuing.

Step 3 - Ask me for the missing details
4. Ask me for:
   - Provider name (DeepSeek / MiniMax / Zhipu GLM / OpenRouter / my own gateway, ...)
   - Base URL (if I don't know it, look it up in the provider's official docs - don't guess)
   - API key
   - Whether I want only specific models (otherwise fetch them all)
   Run `codex-switcher presets` first: if my provider is a built-in preset use --preset,
   otherwise add it as a custom provider with its Base URL.

Step 4 - Configure
5. Use my key with the key passed on stdin:
     codex-switcher add --preset <preset-id> --key-stdin
     codex-switcher add --name "<provider>" --base-url "<url>" --key-stdin
   Never pass the key as a command-line argument, never write it into a file or config.toml,
   and never echo it back to me in chat.

Step 5 - Switch and verify
6. Run `codex-switcher use <provider-id>`, then `codex-switcher doctor` again.
7. If doctor says a local bridge is required, run `codex-switcher bridge --install-agent`.

Step 6 - Tell me three things
8. Which provider and model Codex is now using; the exact command to switch back to official
   OpenAI; and that Codex must be fully quit and reopened for the switch to take effect.
9. Warn me in advance about this: if an existing conversation errors with
   "model is not supported when using Codex with a ChatGPT account", that conversation is
   still pinned to the old provider - `codex-switcher repair` fixes it in place.
10. Finally, ask whether I want a double-clickable GUI installed too
    (macOS: `bash packaging/macos/build_app.sh --with-python`).
```

---

## 它为什么这样设计 / Why the prompt is written this way

- **密钥只走标准输入。** `--key-stdin` 让密钥不进入 shell 历史、不进入进程列表；
  AI 也不该把它回显到对话里。密钥最终只落在系统密钥库
  （macOS 钥匙串 / Windows DPAPI / Linux Secret Service）。
- **不让 AI 猜 Base URL。** 猜错的地址会得到一个看着能连、实际走错地方的配置。
  预设里已经内置了 16 个平台的准确地址（`codex-switcher presets`）。
- **最后一问是图形界面。** 命令行够用，但双击打开的 App 更适合日常切换；
  完整版自带 Python，装到别的电脑上也不用配环境。
