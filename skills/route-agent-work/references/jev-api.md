# TypeSafe Jev

Jev is never a routing judge: the `delegate` / `select` judgments use only Sonnet 5 low or GPT-6.1-sol low on the
user's subscription (see the [low-effort judge workflow](judge-workflow.md)).

Jev may be selected as the **System-One task scorer** (`scorer_type: jev`): `route_judge.py` then sends
`POST {base_url}/v1/systemone` (default `https://api.typesafe.ai`, model `jev-latest`) with
`Authorization: Bearer <key>`, asking `choice` questions for `work_kind` and `interaction_style`, `score` questions
for the six profile levels, and `noul` questions for `needs_visual`, `needs_gui` and `local_only`. The key comes from
`~/.route-manager/scorer-config.json` (`jev_api_key`), else `TYPESAFE_API_KEY`, else `JEV_API_KEY`; it never lives in
the repository. A missing key or failed call falls back to agy (unless `fallback_to_agy` is false), then to the
deterministic profile. Configure it with `route_judge.py scorer --action set --type jev --api-key -` or in
AgentCompass under Settings > System options. `scripts/jev_route.py` is a compatibility entrypoint to `route_judge.py`.
