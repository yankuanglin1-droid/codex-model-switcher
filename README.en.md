# English README moved

> **v1.7.10 history protection:** startup, default-provider switching and background checks no longer rewrite existing histories or task bindings. Existing tasks and automations retain their provider; new tasks use the new default. Earlier automatic migration/cleanup behavior is retired. See [history preservation](docs/history-preservation.md).

**v1.7.8:** frosted macOS startup screen, corrected official-provider diagnostics,
safer process shutdown, and [Windows edition/build status](docs/windows.md).

**v1.7.7 — built-in history repair.** Repair foreign message IDs when returning
 to OpenAI directly in the app, with backup, progress and safe restart.
 Read the [compatibility audit and remaining limits](docs/debug-audit-v1.7.7.md).
 Automation diagnostics distinguish fixed-model schedules from heartbeats;
 successful scheduled execution must still be verified in the host app.

The English documentation is now the main README:

**→ [README.md](README.md)**

中文介绍在 [README.zh-CN.md](README.zh-CN.md)。
