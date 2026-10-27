# Antigravity Resilient

Antigravity Resilient registers the `antigravity-resilient` provider in Paseo. It wraps the Antigravity CLI (`agy`) with durable timeline persistence across daemon restarts and bounded recovery for transient CLI failures.

## Features

- Preserves native `conversationId` session continuation (`--conversation <id>`) and replays stored timeline items when Paseo reopens a session with `history: "replay"`.
- Stores normalized timeline snapshots and append-only event logs per conversation under `~/.paseo/antigravity-resilient/conversations`, with fallback reconstruction from `~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript.jsonl`.
- Retries once per turn when an hourly OAuth token refresh race returns `UNAUTHENTICATED (code 401)` or a token endpoint timeout.
- Completes turns cleanly when `agy` reports a historical sticky stream-interruption trajectory error alongside a valid final response, and retries once when a stream interruption aborts a turn before completion.
- Refreshes the model catalog when the `agy` binary changes or when a newly selected model is missing from the cached catalog.
- Automatically compacts conversation context (`/compact`) **1 minute before the 5-minute hot prompt cache expires** (after 4 minutes of idle time) when context usage reaches `>= 150,000` tokens (`150K`), with Claude Code's prompt-cache warmth/lapsed gate, post-compaction token isolation, rapid-refill thrashing circuit breaker (3 consecutive refills within `< 10` agy model requests each), and consecutive-failure circuit breaker (3 failures).
- Reports session context usage (`session.usage` with both `contextWindowUsedTokens` and `contextWindowMaxTokens`, read from each model request's step usage) so Paseo's context-window meter renders instead of showing "no data". After any compaction (idle, manual or agy's own checkpoint) the meter drops to an estimate of the compacted size until the next request reports the real one.
- Registers the `Antigravity` **usage source**, reading the signed-in account's subscription quota from the Antigravity quota summary endpoint and surfacing its windows (Gemini and Claude/GPT weekly plus 5-hour buckets) with per-group detail rows.

## Requirements

- Paseo `>=0.11.0` with plugins enabled.
- Antigravity CLI (`agy`) installed on the host and signed in.

## Limitations

- Exposes only the `full-access` mode (`--dangerously-skip-permissions`), matching Paseo's built-in Antigravity provider because `agy` cannot interactively prompt for tool permissions when driven externally.
- Single-retry recovery applies only to transient `401 UNAUTHENTICATED` token refresh gaps and mid-turn stream interruptions. Genuine expired credentials fail after one retry and require signing in with `agy`.
- Session history replay reconstructs items recorded by this provider or present in the local Antigravity `transcript.jsonl` log on the same host.
- `agy` publishes no per-model context limits, so the context-window maximum is a per-family estimate (`gemini-*` → `1,048,576`, `claude-*` → `200,000`, `gpt-oss-*` → `131,072`, otherwise `1,000,000`). Set `providerOptions.contextWindowMaxTokens` to override it when the meter must be exact.
- The usage source reads the Antigravity credential from the OS credential store (Windows Credential Manager, macOS Keychain) and never refreshes it: when the hourly token is expired the widget asks you to reopen `agy` rather than rotating your login. Linux credential stores are not supported yet, so the widget reports no quota there.
