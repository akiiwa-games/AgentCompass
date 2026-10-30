# Fast launch contracts

Verified 2026-10-30. Fast preserves the selected model and effort. It does not
switch Sonnet/Haiku to Opus. Pi has no implemented fast feature and no modeId.
The effective model policy's explicit `fast_capable: false` disables fast even
for a verified pair. An absent flag does not disable a verified pair. Automatic
selection, explicit records and launch revalidation honor this same opt-out.

| Harness | Verified model IDs | Paseo creation | CLI creation |
|---|---|---|---|
| Codex | gpt-6.1-sol, gpt-6.1-terra, gpt-6-astra, gpt-6-sol, gpt-6-luna | features.service_tier = priority/default | -c features.fast_mode=true/false and -c service_tier="priority"/"default" |
| Claude | claude-opus-5-5, claude-opus-5, claude-opus-4-8 | features.fast_mode = true/false | --settings '{"fastMode":true/false}' (CLI >= 2.1.205) |

Normal launches explicitly pass the off/default value to avoid inheriting saved
global preferences. These settings are session-scoped; the router does not edit
user settings or billing. Continued Paseo sessions retain their chosen feature.
Provider feature state confirms requested configuration, not actual backend speed.
Inspect response usage.speed for Claude, where available. Fallback/cooldown/account
restrictions can return standard speed even with the requested feature enabled.

Automatic `--fast` preference and pre-reset sprints apply only to Codex. Public
subscription fast burn defaults to 2.5x; purchased credits use 2x. Explicit local
`scoring.fast_burn_multiplier` calibration still wins for subscription accounting.
API billing follows API pricing rather than the ChatGPT credit multiplier; the
router's API-equivalent estimates are not an invoice.

Claude fast requires an explicit provider/model override with `--fast`. It uses
paid usage credits outside subscription limits, never a subscription surplus or
reset bonus. The router marks subscription fast as `fast_billing: usage_credits` (Console API as
`api`, unknown billing as `unknown_paid`), leaves dollar/percent
budget and credit balance unknown, and creates no fictitious subscription budget
reservation. Existing account/launch protections remain conservative and can
still block a request; this feature is not a bypass for an exhausted account.
The provider enforces credit entitlement. Router never enables credits, purchases
them or changes limits. Sonnet, Haiku and Opus 4.7 are unsupported.

Local evidence: Codex CLI 0.165.0; Claude CLI 2.1.312; installed Paseo advertises
Codex service_tier and Claude fast_mode. Claude adapter forwards fastMode both in
SDK settings and applyFlagSettings. On the verified account a tiny Opus 5.5 call
completed with the requested flag but usage.speed=standard; its cached account
status reported org_level_disabled for extra usage. This is not proof of paid
fast execution or a latency improvement. Other installations need their own
feature/entitlement verification.

Sources: [OpenAI speed](https://learn.chatgpt.com/docs/agent-configuration/speed),
[OpenAI config schema](https://learn.chatgpt.com/docs/config-schema.json),
[Claude Code fast mode](https://code.claude.com/docs/en/fast-mode).
