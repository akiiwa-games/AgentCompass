"""Compatibility entrypoint for route_judge.py.

route_judge profiles tasks with a configurable System-One scorer (`route_judge.py scorer --action show|set|test`):
agy Gemini 3.8 Flash low by default, or TypeSafe Jev (POST /v1/systemone, key from ~/.route-manager/scorer-config.json,
TYPESAFE_API_KEY or JEV_API_KEY), Claude Fable/Sonnet low, Codex Sol low, a custom OpenAI-compatible API, or the
offline heuristic. The delegate/select judges still run only on Sonnet/Sol low subscriptions; Jev is never a judge.
"""

from route_judge import main


if __name__ == '__main__':
    raise SystemExit(main())
