# TypeSafe Jev

Jev may be selected as the **System-One task scorer** (`scorer_type: jev`): the router then sends
`POST {base_url}/v1/systemone` (default `https://api.typesafe.ai`, model `jev-latest`) with
`Authorization: Bearer <key>`, asking 10 concrete questions: `choice` questions for `work_kind`, `secondary_kind`,
`effort_band`, `access` and `task_count` (`1`, `2`, `3`, `>3`), `score` questions for `primary_level` (`1..3`) and
`secondary_level` (`0..3`), and `noul` questions for `local_only`, `needs_visual` and `wants_fast` (`axis_demand`,
`needs_gui`, `read_only_review` and `multi_task` are derived deterministically in `task_intake.validate_profile`). The key comes from
`~/.agent-compass/scorer-config.json` (`jev_api_key`), else `TYPESAFE_API_KEY`, else `JEV_API_KEY`; it never lives in
the repository. Without a key, Jev is skipped. A failed call moves on to the next scorer in the cascade
`claude_haiku_low`, `codex_terra_low`, `agy_flash_low` (only the configured scorer is tried when `fallback_to_agy`
is false), each on every ready account of its family. When all of them fail or are absent, `score_task` returns no
profile and the scorer metadata `source: scorer_unavailable` with the reason; no keyword-based or default profile is
made up. A task the scorer refuses as too large (HTTP 413) is remembered, and the remaining scorers are still tried; only when none of them answers does `score_task` raise that error instead of returning `scorer_unavailable`. Configure it with `agentcompass scorer --action set --type jev --api-key -` or in
AgentCompass under Settings > System options.
