---
name: chatgpt-pro-consult
description: Carry out a route-agent-work-authorized Pro consultation or plan critique through the isolated codex-chatgpt-web Browser-only bridge, with recorded submission and complete answer capture. Use for GPT Pro or 6pro reasoning queries; return advice, not execution or approval.
---

# ChatGPT Pro Consult

Default target: GPT-6 Pro at the highest effort available within Pro, unless the assignment names
another target. This uses the user's ChatGPT web subscription through a local Browser-only bridge.
Never silently switch to a billed API, a Codex model, another account, or a different prompt.

This is a leaf. `$route-agent-work` owns whether to consult, the destination and outbound scope,
parent decisions and integration. Do not spawn agents, explore broad project scope, modify product
files, or present advice as independent review or acceptance.


## Host dispatch availability

Before calling this runner directly, run `python -X utf8 scripts/host_policy.py --account chatgpt-web`
from the installed sibling `route-agent-work` skill and require exit 0. It reads the optional
`~/.route-manager/host-policy.json` / `ROUTER_HOST_POLICY` override. A denied account or invalid
profile blocks this dispatch only; Self may continue other work. A recorded user constraint does
not bypass host availability. Router authorization is still required.

## Accept the assignment

Establish the reasoning question, current claim/design and fixed premises, available facts,
assumptions and gaps, desired insight, integration point, constraints and authorized outbound
material. Prose is enough. Missing facts are fine when the question is how to identify them, and
the router's decision to consult is not re-litigated. Use the smallest useful context. If the
question or permitted transfer is unclear, return the specific gap to the parent with prepared work
intact; never infer broader permission.

Every outgoing prompt, summary, bundle, manifest, attachment and substantive follow-up falls under
the router's [transfer rules](../route-agent-work/SKILL.md#4-dispatch-authority-and-context).
Credentials, authentication material, private keys, personal/regulated or live financial account
data are never inputs. A leaf cannot broaden or substitute the package; return proposed changes to
the parent. Existing session authorization covers revisions within its scope.

A plain question needs no files. To bundle selected local files, use
`scripts/build_consult_packet.py` ([packets](references/browser-workflow.md#packets-from-selected-files));
report a rejected input instead of bypassing its credential or path checks.

## Consult through the bridge

Use [the bridge client](references/consult-bridge.md) first. It sends only the selected prompt
through the Browser-only bridge and returns advice. It launches no worker, exposes no tools and
inherits no transcript. A returned Pro alias does not prove an exact version; keep live model
evidence separately.

For a plan consultation, supply the goal, constraints, evidence and current proposal, and ask about
alternatives, weak assumptions, failure conditions and verification. A Pro answer is not execution,
approval, or evidence that suggested checks ran. The transport choice never adds consultations.

## Browser fallback

Use [the browser workflow](references/browser-workflow.md) only when the bridge is unavailable
before submission, recovery shows nothing was submitted, or a required attachment cannot go through
the text-only bridge. Recover an unresolved request before any other transport sends it. Invariants:

- Verify the target model at its highest Pro effort from live controls; never substitute.
- One owned tab, one request, one recorded send. An uncertain send is inspected, never retried.
- Never click answer-now or stop generation, or resend to escape latency.
- Accept only a new, stable, complete answer tied to this request; save it, then
  [finish and release control](references/browser-workflow.md#finish-and-release-control).

Never inspect cookies, stored credentials, browser profiles or private backend endpoints to recover
authentication or force model selection.

## Integrate the answer

Check that the answer addresses the question, supports its conclusion, separates facts from
inference, exposes assumptions, gaps and falsifiers, and respects constraints. A conclusion that
adds no insight is acceptable. Keep useful partial reasoning and report omissions. Mark a key
conclusion unusable if it rests on unsupported facts, answers another question, violates the task
boundary, or cannot be separated from a false approval claim. Formatting is not validity.

Correct a truncation or misunderstanding within the same question and package; stop repeating
ineffective corrections. New questions, evidence or attachments go back to the parent to select the
package and assess authorization; a new turn does not by itself require renewed consent.

Return the conclusion, any change to the supplied understanding (including none), important flaws
and assumptions, next analysis and the verification the parent still needs, plus useful artifact
paths or conversation URL and requested versus observed model/effort. Use the parent's format if
given. Status is `ok`, `partial`, `blocked`, `failed` or `unusable`, naming the affected reasoning
or operation. Describe what Pro suggested, challenged or identified; never say it approved,
validated or accepted anything.

## Missing login, model or Pro

Preserve the prepared request and report the specific missing capability. Start the bridge only
through its configured launcher; if login is needed, ask the user to sign in through the bridge's
own profile. Never silently change model, effort, account or provider. Login is a one-time
prerequisite per session, not a per-query confirmation. Browser recovery steps are in
[the browser workflow](references/browser-workflow.md#resume-and-recover).
