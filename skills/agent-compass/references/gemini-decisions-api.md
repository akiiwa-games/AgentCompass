# Gemini Decision 1 (Decisions API)

Google's Gemini Decisions API answers closed questions (yes/no, one option, a rating) about a shared input with
calibrated probabilities and no generated text. Reference: <https://aistudio.google.com/docs/decisions-api>
(Gemini Developer API, `v1beta`, model `gemini-decision-1`).

Like [TypeSafe Jev](jev-api.md), it is never a routing judge: the `delegate` / `select` judgments use only the
subscription judges (see the [low-effort judge workflow](judge-workflow.md)). It may be selected as the
**System-One task scorer** (`scorer_type: gemini_decision`): the router then sends
`POST {base_url}/v1beta/decisions` (default `https://generativelanguage.googleapis.com`, model `gemini-decision-1`)
with the key in the `x-goog-api-key` header. The request carries:

- `input`: the whole task text and the dimension rubric, one JSON object serialized as text (the API takes a JSON
  object only as text). Nothing is cut or summarized.
- `questions`: the same question set Jev gets. `choice` questions for `work_kind`, `interaction_style`, the effort
  band, urgency, `access` and `task_count` (`1`, `2`, `3`, `>3`); `score` questions for the profile levels and the
  four category demands; `noul` questions for `needs_visual`, `needs_gui`, `local_only`, `wants_fast`,
  `read_only_review` and `multi_task`. The model never sees a question key, so every instruction is a whole question.

The answers become the task profile the same way as Jev's: a `choice` gives the option, a `score` the
probability-weighted level rounded to a level, a `noul` is true from 0.5. The answer names no model, so the scorer
metadata reports the configured one. The per-answer `confidence` is not used: no threshold of our own turns it into a
profile field.

Key, cost and when it runs:

- The key comes from `~/.agent-compass/scorer-config.json` (`gemini_api_key`), else `GEMINI_API_KEY`; it never lives
  in the repository, and it is never sent to another scorer's endpoint.
- The API bills input tokens (no output tokens). It runs only when it is the chosen scorer type: a key alone,
  in the file or the environment, never adds it to the cascade.
- Without a key it is skipped. A failed call (HTTP 400 covers a limit, a blocked input and a rejected key) moves on to
  the next scorer in the cascade `claude_haiku_low`, `codex_terra_low`, `agy_flash_low` (only the configured scorer
  is tried when `fallback_to_agy` is false). When none answers, `score_task` returns no profile and
  `source: scorer_unavailable`; no keyword-based or default profile is made up.
- It answers task profiles only. Other classifier questions (free-form schemas) go to the next scorer in the cascade.

Configure it with `agentcompass scorer --action set --type gemini_decision --api-key -` (the key is read from stdin)
or in AgentCompass under Settings > System options, and check it with `agentcompass scorer --action test`.
