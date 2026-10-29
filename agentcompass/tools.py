"""Why an optional host executable is needed, and how to install it."""

from __future__ import annotations

HINTS = {
    "uv.exe": {
        "tool": "uv",
        "feature": "Dot2API and Muse2API (install and run their Python tools)",
        "install": "Install uv from https://docs.astral.sh/uv/ and put it on PATH.",
    },
    "bun.exe": {
        "tool": "bun",
        "feature": "the ChatGPT Web bridge (build and run its launcher)",
        "install": "Install Bun from https://bun.sh and put it on PATH, or place bun.exe at tools/bun/bun.exe in a checkout.",
    },
    "cloudflared.exe": {
        "tool": "cloudflared",
        "feature": "the Dot2API public quick tunnel",
        "install": "Install cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ or run: winget install --id Cloudflare.cloudflared",
    },
    "node.exe": {
        "tool": "node",
        "feature": "Paseo worker dispatch (npm --prefix setup/paseo-bridge ci)",
        "install": "Install Node.js from https://nodejs.org or run: winget install OpenJS.NodeJS.LTS",
    },
}


def hint(name: str) -> dict[str, str]:
    found = HINTS.get(name)
    if found:
        return found
    stem = name[:-4] if name.lower().endswith(".exe") else name
    return {
        "tool": stem,
        "feature": "an optional local feature",
        "install": f"Install {stem} and put it on PATH.",
    }
