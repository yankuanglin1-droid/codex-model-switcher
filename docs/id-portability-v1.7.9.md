# History ID compatibility repair (v1.7.9)

## Root cause

Responses item IDs and tool correlation IDs serve different purposes. A third-party history can contain item IDs ending in `_msg` or `_fc_0`; OpenAI validates prefixes by item type. Fixing only message IDs exposes the next invalid function-call ID on retry.

The repair now normalizes message (`msg`), function-call (`fc`), and reasoning (`rs`) item IDs deterministically. It never changes `call_id`, arguments, message content, tool results, or reasoning content. Existing matching prefixes and unknown item types are preserved. This repairs identifier syntax, not unsupported tools or provider-specific encrypted reasoning.

## Additional fixes

- Visit both raw response records and compacted replacement/guardian histories during explicit migration to OpenAI.
- Version the sweep ledger so earlier checks cannot hide newly detectable invalid IDs.
- Dry-run reports ID changes without modifying files or making backups.
- Do not cache busy or invalid files as successfully checked; isolate per-file validation failures.
- Preserve UTF-8 upstream error response bytes when calculating HTTP Content-Length.

## Usage

After updating, open the app and select **Repair official history & restart**. Save ongoing work first. The app must close ChatGPT/Codex before modifying open history files, then backs up and repairs official-provider histories and relaunches the host. A refused exit means no repair occurred. Merely updating the application does not rewrite histories held by the host.

## Verification and limits

229 Python tests passed on macOS, including call/output correlation preservation, both compaction history containers, idempotence, backup fidelity, blank-line preservation, dry-run immutability, and migration integration. No real Windows run or successful replay of every third-party history is claimed. Server-specific validation can still require additional adapters. No private histories or credentials are included in these tests.
