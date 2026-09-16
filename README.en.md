# Codex Model Switcher

Plug DeepSeek, MiniMax, Zhipu GLM, Kimi, Qwen, SiliconFlow, OpenRouter, Groq and more
into Codex, pick them from the model dropdown, and switch back to official OpenAI any time.

```bash
bash install.sh
echo "$YOUR_API_KEY" | codex-switcher add --preset deepseek --key-stdin
codex-switcher use deepseek
codex-switcher restore          # go back to official OpenAI
```

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

- 17 built-in provider presets plus a fully manual "any Base URL" path
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
python3 -m unittest discover -s tests
python3 tools/verify_providers.py
python3 tools/verify_codex_catalog.py
python3 tools/verify_bridge.py
```

The last three drive a real Codex process. See `README.md` (Chinese) for the full guide.

## License

MIT
