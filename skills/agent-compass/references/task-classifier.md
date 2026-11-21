# System-One Task Classifier

The System-One classifier turns a factual task description into a `TaskProfile`; the allocator then scores candidates
against it. What a parent agent may put in a task, and that it never classifies the task itself: [router internals,
Plan Only](router-internals.md#a2-plan-only-diagnosis). How the profile's demand becomes fit, quota cost, the net score
and the order between equal scores (`candidate_ranking.five_axis_order`): `docs/technical-reference.md` section 1 in the
repository; the gates and terms as the allocator applies them: [router internals, section 2](router-internals.md#2-how-the-allocator-scores-candidates).
The work kinds and their demand table: [`work-kinds.md`](work-kinds.md).

## 1. Commands

| Stage | Command | Model / Engine | When to Use |
| :--- | :--- | :--- | :--- |
| **`score`** | `agentcompass score --input task.json` | The configured System-One scorer cascade (`jev` when keyed -> `claude_haiku_low` -> `codex_terra_low` -> `agy_flash_low`, led by the chosen scorer type when that is another one; returns `status: "blocked", error: "scorer_unavailable"` when all fail) | Standalone inspection and calibration testing of the System-One classifier. |
| **`scorer`** | `agentcompass scorer --action show\|set\|test` | Reads/writes `~/.agent-compass/scorer-config.json` (`ROUTER_SCORER_CONFIG`, type override `ROUTER_SCORER_TYPE`) | Choose the scorer: `agy_flash_low`, `jev` (TypeSafe `POST /v1/systemone`, key `jev_api_key` / `TYPESAFE_API_KEY` / `JEV_API_KEY`), `gemini_decision` ([Gemini Decisions API](gemini-decisions-api.md) `POST /v1beta/decisions`, key `gemini_api_key` / `GEMINI_API_KEY`; runs only when chosen), `claude_haiku_low`, `codex_terra_low`, `openai_compatible`. Keys are printed masked only; `--api-key -` reads the key from stdin. |

## 2. System-One Classifier Output Schema (`TASK_PROFILE_SCHEMA`)

Produced automatically by `score_task` (via the System-One scorer cascade grounded in [`classifier-cases.json`](classifier-cases.json)). There is **no offline keyword fallback**: when all System-One classifiers are unavailable, `score`, `plan`, `roll`, and `tray.router_data.simulate` return `status: "blocked"`, `choice: "none"`, `error: "scorer_unavailable"`.

- `work_kind`: One of the 11 atomic routes (`creative_ideation`, `architecture_reasoning`, `code_implementation`, `frontend_ui`, `code_audit`, `exploration_forensics`, `web_research`, `visual_analysis`, `text_processing`, `live_loop`, `gui_control`).
- `primary_level` (`1..3`): Task depth (`1=routine/localized`, `2=standard module/analysis`, `3=frontier/coupled/fine-grained`).
- `secondary_kind` (`"none"` or a distinct atomic `work_kind`) & `secondary_level` (`0..3`): Optional second domain for composite tasks (`secondary_level = 0` when `secondary_kind == "none"`).
- `access`: `"read-only" | "write"` (`code_audit` is always `"read-only"`, even when saving a review report file; local repository `code_implementation` and `frontend_ui` are always `"write"`).
- `local_only` (`bool`), `needs_visual` (`bool`)
- `wants_fast` (`bool`): `true` only when the assignment affirmatively requests fast mode, priority tier, urgent/ASAP turnaround, or a pre-reset quota sprint (`"pre-reset sprint"`, `"before quota resets"`, `"重置前冲刺"`) for the task itself without negation (`"do not use fast mode"` or `"no rush"` -> `false`; runtime step adverbs such as `"click Save immediately"` -> `false`). Precedence in `normalize_task`: explicit caller `needs.fast` -> `task.fast` -> `task.wants_fast` -> `profile.wants_fast` -> `false`.
- `expected_hours` (`0.05..24.0`), `task_count` (`"1" | "2" | "3" | ">3"`)
- `reason` (optional): One-sentence justification when supplied by a caller profile.
- **Deterministic projections in `task_intake.validate_profile`**:
  - `axis_demand` (`0..3` over `creativity`, `coding`, `aesthetics`, `tool_operation`): projected via `task_intake.project_axis_demand(work_kind, primary_level, secondary_kind, secondary_level)` (the table and the envelope: [`work-kinds.md`](work-kinds.md)). Economy is not a task demand: the router prices it from cost and live quota.
  - `needs_gui` (`bool`): `true` when `"gui_control"` is in `(work_kind, secondary_kind)` (also promotes `needs_visual = true`, as does `"visual_analysis"`).
  - `read_only_review` (`bool`): `true` when `work_kind == "code_audit"`, used by `roll` and `plan_stage` to enforce independent reviewer selection so authoring workers never self-review.
  - `multi_task` (`bool`): `task_count != "1"`.

### Cross-Repo Sync Notes (for Magpie & Cindy)

| Field | Type | Semantic Contract |
| :--- | :--- | :--- |
| `wants_fast` | `boolean` | Replaces `FAST_REQUEST_WORDS` / `infer_fast_preference(objective)`. Set by System-One classifier (`TASK_PROFILE_SCHEMA` boolean / question-API `noul` question) or explicit caller flag (`--fast`, `needs.fast`, `task.fast`, `task.wants_fast`). |
| `read_only_review` | `boolean` | Derived from `work_kind == "code_audit"` (or explicit caller boolean). Gates independent-reviewer worker reuse so authoring workers never self-review. |
| `local_only` | `boolean` | Replaces `OBJECTIVE_LOCAL_RUNTIME_RE`, `LOCAL_MACHINE_RE`, `BOGUS_LOCAL_ONLY_RE`, and `valid_local_only_reason`. |
| `multi_task`, `task_count` | `boolean`, `"1"\|"2"\|"3"\|">3"` | Replaces `MULTI_TASK_MARK_RE` / `multi_task_marks(objective)`; `multi_task` is `task_count != "1"`. |

The classifier's `work_kind` and `primary_level` override a caller's, and its `local_only: true` applies when the caller
left `needs.local_only` unset: [router internals, Classifier Authority Rule](router-internals.md#a2-plan-only-diagnosis).
Explicit caller values (`needs.local_only: true` or `false`, or `--local-only "<reason>"`) are respected directly without regex/length filtering.

Route `implies` fill `local_only` and `visual` only when both the caller and the profile left that flag unset. A classifier `local_only: false` on `exploration_forensics` does not exclude Dot.

`needs_gui` adds the visual requirement. It rewrites `work_kind` to `gui_control` only when the profile did not name a real route and the caller kind is missing or generic (`code_implementation`, `other_or_mixed`). A classifier route such as `code_implementation` stays that route.
