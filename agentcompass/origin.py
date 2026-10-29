"""Public GitHub coordinates shared by onboarding, plugin clones, and release checks."""

from __future__ import annotations

import os
import re

DEFAULT_ORG = "akiiwa-games"
PRODUCT_REPO = "AgentCompass"
RELEASES_API = f"https://api.github.com/repos/{DEFAULT_ORG}/{PRODUCT_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{DEFAULT_ORG}/{PRODUCT_REPO}/releases/latest"

_ORG_ENV = "ROUTE_MANAGER_GITHUB_ORG"
_USAGE_FLOAT_ENV = "ROUTE_MANAGER_USAGE_FLOAT_REPO"
_REMOTE = re.compile(r"github\.com[:/]([^/\s\"']+)/")


def github_org(environ: dict | None = None, git_config: str | None = None) -> str:
    """Env override, else the origin org recorded in a git config, else the public org."""
    environ = os.environ if environ is None else environ
    org = (environ.get(_ORG_ENV) or "").strip()
    if org:
        return org
    if git_config:
        match = _REMOTE.search(git_config)
        if match:
            return match.group(1)
    return DEFAULT_ORG


def sibling_repo(name: str, environ: dict | None = None, git_config: str | None = None) -> str:
    return f"https://github.com/{github_org(environ, git_config)}/{name}.git"


def usage_float_repo(environ: dict | None = None, git_config: str | None = None) -> str:
    """Explicit clone URL, else ``<org>/usage-float``."""
    environ = os.environ if environ is None else environ
    explicit = (environ.get(_USAGE_FLOAT_ENV) or "").strip()
    if explicit:
        return explicit
    return sibling_repo("usage-float", environ, git_config)
