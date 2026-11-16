AgentCompass v0.6.3 adds Google's Gemini Decision 1 as a task classifier you can choose. It builds on v0.6.2.

- **Gemini Decision 1 (Decisions API) as the task classifier.** The classifier (the System-One scorer) reads a task and returns the profile the router allocates on. You can now pick `gemini_decision`: the router sends the task text and its fixed question set to Google's [Gemini Decisions API](https://aistudio.google.com/docs/decisions-api) (`POST /v1beta/decisions`, model `gemini-decision-1`) and builds the profile from the answers.
  - Choose it in the tray under Settings > System options, or with `agentcompass scorer --action set --type gemini_decision --api-key -` (the key is read from stdin). The key can also come from `GEMINI_API_KEY`. `agentcompass scorer --action test` classifies one sample task with it.
  - The API bills input tokens, so it runs only when it is the chosen scorer type. A key alone, in the file or the environment, never adds it to the cascade.
  - When it fails or has no key, the other scorers are tried in turn (`claude_haiku_low`, `codex_terra_low`, `agy_flash_low`), as with any chosen scorer. It answers task profiles only; other classifier questions go to the next scorer.
  - Not yet checked against the live API: the request and the answer parsing follow the published reference and are tested against fixtures in that shape.
- **For contributors.** The question set and the answer parser are shared by the two question APIs (TypeSafe Jev, Gemini Decision 1); the `JEV_*` question tables in `scorer_client.py` are now `PROFILE_*`, and `parse_jev_response` is `parse_profile_answers`.

The Windows tray installer is unsigned; Windows SmartScreen may warn on first launch. See [packaging/SIGNING.md](https://github.com/akiiwa-games/AgentCompass/blob/v0.6.3/packaging/SIGNING.md).

Status: alpha. Accounts come from discovery on your machine; choose which ones take part in the tray.
