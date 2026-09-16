# 让 AI 帮你装（一键指令）/ Let an AI install it for you

不用自己敲命令。把下面整段复制给 **Codex / Claude Code / Cursor / 任何能执行命令的 AI**，
它会自己问你要缺少的信息，然后装好、配好、验证完，最后告诉你按哪个键切换。

Just paste the whole block into **Codex / Claude Code / Cursor / any agent that can run shell
commands**. It will ask for what it is missing, do the install, verify, and tell you how to switch.

---

## 中文指令（复制这一段）

```text
请帮我在本机装好并配置「codex（ChatGPT App）多平台模型切换」
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
