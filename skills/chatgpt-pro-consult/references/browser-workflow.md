# Browser fallback workflow

Controllers, model selection, the state checker, sending, waiting and capture, recovery, packets
and cleanup. The consultation target stays fixed whatever model executes this. Selectors and labels
below are hints: they change over time and depend on locale, so discover the live page first.

## Controllers

Use the executor's actual tool inventory and native shell. A child does not inherit its parent's
browser tools or login, and a model name does not establish tool availability; report the exact
missing tool or startup error instead of retrying in an unsuitable REPL. If a child host lacks the
desktop browser tool, [launch_cua_mcp.py](../scripts/launch_cua_mcp.py) `--desktop-home <codex-home>`
exposes the desktop-installed CUA MCP server to it; only a new session sees newly added tools.

This is unattended automation of a connected, signed-in browser, not a headless process. A first or
expired login may need the user; never repeat login or per-query confirmation while the session is
valid.

Default: the user's signed-in Chrome through its extension MCP (e.g. the Codex `cua` REPL). Start
with one documented entrypoint such as `await cua.getState();`, read the returned documentation,
pick the intended profile from observed metadata, and create a dedicated tab with the observed ID:

```javascript
let consultTab = await cua.createBrowserTab(chromeId, 'https://chatgpt.com/', {sessionName: 'Pro consult'});
```

Keep `consultTab`, the exact prompt, baseline turns and conversation URL in the persistent REPL.
Agent-created tabs may auto-close at turn end; `markHandoff()` a tab that must survive. Prefer DOM
or Playwright controls and compact state reads; use screenshots, coordinates or desktop control
only when needed and permitted. A unified entrypoint is not evidence that desktop input was acquired.

Paseo browser tools (`browser_new_tab`, `browser_snapshot`, ...) are optional: use them when
explicitly selected or when Chrome cannot serve the task. Their background browser has its own
login, refs expire when the page changes, and page evaluation may be rejected by CSP; then use
snapshot/ref tools and never disable CSP. A disconnected host and a signed-out account are different
problems; report the one observed. A controller failure calls for repairing that operation, not an
automatic platform switch or another login.

## Select the model and maximum effort

Choose among the observed menu controls directly, then verify the result with the checks below.

- Read one live snapshot to locate the composer, model control and response structure. The model
  control may appear only after hydration; re-observe once before treating it as absent.
- Select Pro and its highest offered setting. A slider may accept keyboard control; use a coordinate
  only when there is no actionable accessible target. Never assume the rightmost or a number means
  Pro, do not stop at High/Very high when Pro is above them, and recheck after changing the base
  model because the effort can reset.
- The model submenu may show only a "latest" alias. Close it and read the composer label: an explicit
  label such as `6 Pro` establishes model and Pro together. If only an alias remains, record
  `requested_model`, `observed_model` and `model_verified: false`, and report it.
- A plan badge or model self-report does not prove the selection, and API effort strings are not
  browser controls. A final message's `data-message-model-slug`, when present, is an extra drift
  check; never fetch hidden backend data.
- Do not open menus or change the model while a request is generating.

## Fill, send and the state checker

One owned tab, one request: `observe -> select -> prepare -> check -> attempt once -> identify new
user turn -> wait -> check stability -> capture`. Use a fresh tab for a new question and the recorded
conversation for a focused follow-up or recovery. Never touch unrelated tabs or run concurrent
prompts in one tab.

Before filling, confirm the composer is empty or already holds this request; preserve an unrelated
draft. Use unique, composer-scoped semantic controls: a duplicate locator is an ambiguity to
inspect, not a reason for `.first()`, a cached index or a guessed coordinate. A long fill may render
as a pasted-text tile, which submits normally.

```javascript
await consultTab.playwright.locator('#prompt-textarea').fill(authorizedText);
await consultTab.getAXState();
// next call, after inspecting the filled prompt and the current send control:
await consultTab.playwright.getByRole('button', {name: observedSendLabel, exact: true}).click();
await consultTab.getAXState();
```

Paseo equivalents are `browser_fill`, `browser_upload` and `browser_click` with fresh refs.

Run [check_consult_state.py](../scripts/check_consult_state.py) (standard library only) before the
send, after an ambiguous send, and before capture:

```powershell
python -X utf8 <skill-dir>/scripts/check_consult_state.py <task-temp>/state.json
```

Keep the state file in a task temp directory and fill it only from fresh DOM/snapshot data: never
invent positive values, copy the expected model into observed fields, turn missing evidence into an
empty list or false, or mark clipped data `capture_complete`.

| Object | Fields |
| --- | --- |
| Top level | `phase`: `before_send`, `after_attempt`, or `completion`; `request`; `observation`; optional `previous_answer` |
| `request` | `browser_id`, `tab_id`, `prompt`, `model_label`, `attachment_names`, `baseline_user_ids`, `baseline_assistant_ids`, `send_attempted`; optional `conversation_url`, `response_model_slug` |
| `observation` | `browser_id`, `tab_id`, `url`, `capture_complete`, `user_turns`, `assistant_turns`, `generating`; optional `error_text` |
| Send observation fields | `model_label`, `composer_count`, `composer_text`, `send_count`, `send_enabled`, `attachment_names`, `attachments_ready` |
| Each user turn | `id`, complete `text`, `attachment_names` |
| Each assistant turn | `id`, `after_user_id`, answer-body `text`, `final_actions`; optional `model_slug` |
| `previous_answer` | Same candidate's `id` and full answer-body `text` from the previous observation |

IDs are the controller's real strings; `after_user_id` is the nearest preceding user turn in the DOM.
`generating` is true/false only when current controls establish it, otherwise null. Save
`baseline_*_ids` before filling; persist `send_attempted: true` immediately before the first send
action; never reset the baseline to include a newly submitted turn. For a new chat, record the
stable conversation URL once it replaces the provisional one.

| Action returned | What the executor does |
| --- | --- |
| `ready_to_send` | Recheck live tab, exact draft, model and unique enabled send control immediately before clicking once; record the attempt first. |
| `submitted` | Record the new user ID and actual conversation URL; continue waiting. |
| `wait` | Keep the same request; observe again after an appropriate interval. |
| `capture` | Extract and save the full identified answer with links and formatting, then finish and release control before returning. |
| `inspect` | Read the reason and repair or re-observe that operation; never send, resend or claim completion on uncertain state. |

Exit code 2 with `inspect` is an operation diagnostic, not a request for user permission. The checker
cannot authenticate observations, verify outbound authorization, or prevent a race between reading
and clicking; keep the final live check and the single click in the same browser tool call.

- Compare the full prompt, not a prefix. The checker normalizes only line endings and trailing
  newlines. Rich-text composers may render newlines as separate paragraphs: join paragraphs with one
  newline, keep empty paragraphs and line breaks, and ignore only a confirmed trailing-break node.
- Read actual attachment names and readiness from the UI; a pasted-text tile's filename is not the
  prompt.
- After any attempted send, a retained draft or tool success is not evidence either way. Inspect the
  same tab for a matching new user turn; never retry or open a replacement chat while submission is
  uncertain.
- A completed older answer never satisfies a new request. Same length and a stable tail do not prove
  stability: compare the same answer ID and full body across observations.

## Wait and capture

Poll every 20–30 seconds with blocking waits under 60 seconds, stay responsive to the parent and give
brief progress updates. Never click answer-now or stop generation, or resend to escape latency; a
deep request can run for minutes with unchanged progress. Discover the message structure once, then
reuse a compact read-only probe.

DOM hints (verify live): user and assistant units carry `data-content-search-unit-key` ending in
`:user` or `:assistant`; use the full key as the message ID. Both may share a
`data-content-search-turn-key`, so associate an answer with its user turn by turn key plus DOM order.
The answer body is the assistant unit's `[data-markdown-text-style="assistant-message"]` descendant,
and final reply actions (copy, rate, share, regenerate) appear after completion. On a continued
conversation, enumerate units and match the exact new prompt and baseline IDs; taking the first unit
is safe only when the conversation has exactly one request.

During generation read only matching IDs, generation/error state, body length and a short tail.
Completion requires a new answer tied to this request, no active generation control, final reply
actions or equivalent evidence, and a stable follow-up observation; an absent stop control alone is
not completion. Capture the body once, preserving code, tables, equations and links and excluding
surveys, menus and thinking-duration labels; check for truncation and save `response.md`. Prefer
targeted text reads to screenshots. Keep the main turn active until the parent can integrate the answer.

## Resume and recover

A small `run.json` can hold the browser ID, conversation URL, requested and observed model/effort,
verification evidence, request/response paths, last user-turn marker and submission state
(`prepared`, `sending`, `submitted`, `completed`); never credentials. Resume with the saved browser
ID if still listed; otherwise open the recorded URL in one new tab and check its last user turn
before reading an answer. Without a captured URL and with ambiguous submission, report the gap
instead of guessing a chat.

- CSP-rejected evaluation: continue with snapshot refs and standard tools; never disable CSP, patch
  the browser host, or keep retrying.
- Tab ownership error: refresh handles; if another session owns the tab, use another controllable
  tab. Repeated claiming is not repair.
- Upload failure: inline the same authorized text when no real attachment is needed; otherwise keep
  the attachment and fix the reported failure using current docs.
- Generation or UI failure: re-observe and recover the same conversation. If that needs unavailable
  controls, return the saved packet and conversation location instead of opening replacement chats
  or changing browser/runtime configuration.

## Packets from selected files

A plain text question needs no bundle. For selected local text files, build one auditable Markdown
packet:

```powershell
python <skill-dir>/scripts/build_consult_packet.py `
  --root <absolute-workspace> `
  --brief <absolute-brief.md> `
  --input <absolute-selected-file-1> `
  --attachment <absolute-image-or-pdf> `
  --output <absolute-packet.md>
```

The script accepts exact files only, records hashes, refuses files outside `--root`, rejects common
credential material, and lists binary attachments without embedding them. Inspect the packet before
transmission. If an input is rejected, keep its path and reason and return blocked to the parent; do
not redact, substitute or add artifacts inside this leaf, and do not weaken the guard. A brief can
state, as far as they affect the question: role and reserved decisions; the precise question and
why direct work does not settle it; the current claim and fixed premises; facts, assumptions, gaps
and rivals; hard constraints; the integration point; the authorized destination, exact outbound
package and excluded material.

## Finish and release control

Answer capture and browser cleanup are separate outcomes: the checker's `capture` action does not
check tab closure or control release. Complete this cleanup before the final handoff without asking
again for routine permission.

1. Save the complete answer and conversation URL outside the browser/REPL. Keep a tab only for an
   actual requested continuation, login or handoff, naming its browser, tab ID, URL, purpose and next
   owner; possible future usefulness is not a reason.
2. Close each owned temporary tab with the controller's documented API (`consultTab.close()`, or
   `browser_close_tab` on Paseo), then refresh the same browser's tab inventory and verify the tab ID
   is absent. Turn-end auto-close, a disconnected browser or a failed inventory is not evidence of
   closure. If a fresh inventory already shows the tab gone, report that; do not reopen it to close
   it or claim a close you did not perform. Leave unrelated tabs untouched.
3. End any task-owned browser or desktop control session with a documented release/stop tool when the
   runtime exposes one, and inspect its result. Use a visible Stop/Release control only if observed,
   owned by this session and permitted. Never invent a release API or treat the page's stop-generating
   button as a release. If no release exists, stop further input and report what remains unverified.
   Do not restart the controller to clear an indicator. Cleanup never includes logging out, closing
   the whole browser, killing helpers or disconnecting shared MCP services.
4. Then clear this task's dedicated control REPL if a reset tool exists (e.g. the CUA REPL reset),
   unless another active task or handoff still needs it. A reset clears bindings only; it neither
   closes tabs, releases control nor proves an indicator disappeared. Do not reacquire control just
   to verify it.

Record tab state, release result or capability limit, and REPL cleanup separately in `run.json` or the
final handoff, with the useful reasoning, conversation URL, requested/observed model and effort, and
answer paths. A saved answer is still delivered when cleanup is unavailable; name the remaining
operation rather than claiming a fully closed session. On cancellation or failure, preserve
recoverable state, clean up only within remaining permissions, and never resume UI input after the
user has stopped computer use.
