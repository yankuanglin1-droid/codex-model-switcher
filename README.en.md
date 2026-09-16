# Codex (ChatGPT App) Multi-Provider Model Switcher

[中文](README.md) · **English** · [Let an AI install it →](INSTALL-WITH-AI.md)

> **macOS: download the app → [latest release](https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest)**
> Pick the `…-full.zip` (~52 MB, bundles its own Python, nothing to install).
> The binaries are not committed to this repo on purpose — a 52 MB zip in git
> would stay in history forever.

Plug DeepSeek, MiniMax, Zhipu GLM, Kimi, Qwen, SiliconFlow, OpenRouter, Groq and more
into Codex, pick them from the model dropdown, and switch back to official OpenAI any time.

```bash
bash install.sh
echo "$YOUR_API_KEY" | codex-switcher add --preset deepseek --key-stdin
codex-switcher use deepseek
codex-switcher restore          # go back to official OpenAI
```

## Install

**Don't want to type commands?** Paste the prompt from
[INSTALL-WITH-AI.md](INSTALL-WITH-AI.md) into Codex / Claude Code — it will ask you for the
provider name and API key, then install, configure and verify everything itself.

### macOS — download the app

Grab a zip from [Releases](https://github.com/yankuanglin1-droid/codex-model-switcher/releases/latest):

| File | Size | Pick it if |
| --- | --- | --- |
| `Codex-Model-Switcher-macOS-v…-full.zip` | ~52 MB | **Recommended.** Ships its own Python — nothing to install, just unzip and double-click |
| `Codex-Model-Switcher-macOS-v….zip` | ~1 MB | You already have Python 3.9+ (Xcode command line tools or Homebrew) |

Both are **universal binaries** (arm64 + x86_64) and run on macOS 11 or newer.

> First launch is blocked by Gatekeeper because the app has no Apple Developer signature.
> Right-click the app → **Open**, once. It won't ask again.

### macOS / Linux — from source

```bash
git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
cd codex-model-switcher
bash install.sh
```

Want the double-clickable window app as well:

```bash
bash packaging/macos/build_app.sh                 # standard (~1 MB, uses system Python)
bash packaging/macos/build_app.sh --with-python   # full: bundles its own Python
```

### Windows

```powershell
git clone https://github.com/yankuanglin1-droid/codex-model-switcher.git
cd codex-model-switcher
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer copies the runtime to `%LOCALAPPDATA%\codex-switcher`, adds
`codex-switcher` to your user PATH and drops a desktop shortcut. Open a **new terminal**
afterwards. The UI opens in your browser on Windows (the native window is macOS-only);
everything else is identical. Keys are encrypted with Windows DPAPI.

## Why

Codex only talks to OpenAI models out of the box. Wiring up another provider by hand means
editing `config.toml`, hand-writing a model catalog JSON, and discovering the hard way that
**current Codex builds reject `wire_api = "chat"`** and only accept `responses`.

This tool does all of it in two commands. The pitfalls it handles:

| Pitfall | What the tool does |
| --- | --- |
| Codex rejects `wire_api = "chat"` and refuses to start | Always writes `responses`; chat-only providers are translated by a built-in local bridge |
| The model catalog has to be written by hand | Auto-fetched from the provider and generated for you |
| Reserved provider ids (`ollama`, `lmstudio`) silently break the model list | Auto-renamed to `ollama-local`, and reserved ids are refused |
| API keys end up in `config.toml` | Keys live in the OS keychain only |
| Editing config wipes plugins/MCP/project settings | Only model keys are touched, with backup and full validation |
| Old threads break after switching | The tool warns you and offers fork/new-thread paths |

## Features

- 17 provider presets, one of which is a fully manual "any Base URL" entry
- Full model list imported per provider, newest versions first
- Switch from CLI, interactive menu, or a local-only web UI
- A native macOS window app (Swift + WKWebView): its own window, Dock icon and menu bar,
  no browser and no Terminal window
- `codex-switcher restore` returns to official OpenAI in one step
- Real balance/quota display where the provider exposes an API; an honest "not available"
  where it does not
- Local token usage stats read from Codex's own session logs (never leaves your machine),
  plus an optional usage percentage against a quota you declare yourself
- Update check: `codex-switcher update`
- Built-in Responses ⇄ Chat Completions bridge
- `codex-switcher doctor` environment check
- Standard library only, no pip installs

## How it works

It edits two things in `~/.codex/config.toml`: the top-level model settings and one
`[model_providers.<id>]` block whose auth is a keychain helper — **no secrets in the file**.
Everything else is preserved byte-for-byte and validated afterwards; a backup is written first.

Providers that only speak Chat Completions are routed through a local bridge on
`127.0.0.1:8787`, because Codex now requires the Responses protocol.

## Verification

```bash
python3 tools/preflight.py              # full pre-release self-check (runs everything below)
python3 -m unittest discover -s tests   # 71 tests, green on Python 3.9 / 3.12 / 3.13 / 3.14
python3 tools/verify_providers.py
python3 tools/verify_codex_catalog.py
python3 tools/verify_bridge.py
```

The last three drive a real Codex process. The full guide, troubleshooting and the
Windows notes live in the [Chinese README](README.md).

## More

- [INSTALL-WITH-AI.md](INSTALL-WITH-AI.md) — a copy-paste prompt that makes an AI agent do the install
- [docs/ai-setup.md](docs/ai-setup.md) — the detailed manual an agent should read: reserved
  provider ids, the local bridge, what counts as evidence of success
- [docs/app.md](docs/app.md) — the macOS app, and why switching does not lose your context
- [docs/troubleshooting.md](docs/troubleshooting.md) — when something goes wrong
- [docs/security.md](docs/security.md) — where your keys live

## License

MIT
