"""Minimal Venice AI chat completion via the OpenAI-compatible API.

Setup:  pip install -e ".[venice]"
Config: put VENICE_API_KEY=... in the repository root .env (gitignored) or the environment.
Run:    python -X utf8 examples/venice_chat.py "Say hello"
Docs:   https://docs.venice.ai
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from openai import OpenAI

BASE_URL = "https://api.venice.ai/api/v1"
DEFAULT_MODEL = "zai-org-glm-5-2"  # GLM 5.2


def load_env(path: Path = Path(__file__).resolve().parents[1] / ".env") -> None:
    """Load KEY=VALUE lines from .env without overriding existing env vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client() -> OpenAI:
    load_env()
    key = os.environ.get("VENICE_API_KEY")
    if not key:
        raise SystemExit("VENICE_API_KEY is not set (root .env or environment).")
    return OpenAI(api_key=key, base_url=BASE_URL)


def main() -> None:
    client = make_client()
    prompt = " ".join(sys.argv[1:]) or "Reply with exactly: pong"
    model = os.environ.get("VENICE_MODEL", DEFAULT_MODEL)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=64,
    )
    print(f"model={resp.model}")
    print(resp.choices[0].message.content)


if __name__ == "__main__":
    main()
