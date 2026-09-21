# v1.7.7 compatibility audit

## Fixed and covered by regression tests

- Foreign message IDs rejected after returning to OpenAI: normalize typed
  message records and compacted histories, preserving text and tool call IDs.
- App repair workflow: graceful quit, background progress, private backups,
  readback verification and reopen; no external repair script required.
- Duplicate restart routes and inconsistent success fields: one async recovery
  contract consumed by both UI entry points.
- AppleScript returning `false` was mistaken for a running process because its
  exit status was zero. Parse its output and fail closed on probe errors.
- Binary Info.plist bundles were missed by text matching. Parse property lists.
- Migration could replace an idle but still-open rollout. Check open handles and
  concurrent modification before migration. This protects future writes; it
  does not reconstruct data already lost by older versions.
- Repair counts no longer increment without a session or database change.

## Validated locally

Python unit/regression suite, JS syntax, release preflight (including repository
secret/history scanning and portability checks), macOS build and archive checks.
Native quit/relaunch tests use process mocks to avoid terminating the live
assistant task. Confirm the real provider accepts the repaired history by
resuming the affected task after the user initiates in-app recovery.

## Limits and follow-up gates

- This release repairs message-ID errors, not all possible Responses schemas.
- Automatic quit/repair/reopen is macOS only. Windows runtime checks here are
  static; Windows UI and scheduler execution require a Windows machine.
- Scheduler diagnostics are read-only. Fixed-model schedules and heartbeats
  require separate binding checks. Real scheduled triggering is not verified.
- Provider-native reasoning, remote tools and model quotas are provider-specific.
- Historical cleaners still remove certain cross-provider tool records. Full
  semantic portability is not established; keep backups and review migrations.
- No guarantee of bug-free operation, restored lost context, or universal model
  and automation compatibility is made.

## Additional regression checks

- Balance refresh previously formed a URL with two question marks, losing the
  authentication query parameter. Request construction now preserves both
  balance and authentication parameters (checked with a mocked JS fetch).
- History cleanup now preserves blank lines, uses atomic replacement and checks
  for an open host handle or concurrent write before replacement.
- The old restart helper no longer falls back to SIGTERM after a cancelled quit.
- Local tests: 216 passed. Native binaries are built for arm64 and x86_64;
  this is build coverage, not physical testing on every Mac generation.

- Live launch exposed stale UI-server reuse after app replacement. The app and
  UI state now carry matching versions; only a verified old switcher UI process
  may be retired. The model bridge is not stopped by this upgrade check.

## v1.7.8 follow-up

The official built-in provider was incorrectly required to have a custom provider
table. This was a local diagnostic false positive, now regression-tested for
explicit and implicit OpenAI, custom overrides, and missing third-party tables.
The Windows launcher now quotes interpreter paths. Process shutdown requires
command-line identity, not just a python.exe process name or a stale PID record.
Unknown identities are preserved for review and not terminated.

Windows runtime validation is pending GitHub workflow permission; see windows.md.
