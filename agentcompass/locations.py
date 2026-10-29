"""Find the skill and setup files, whether this is a checkout or an installed wheel."""

from __future__ import annotations

from pathlib import Path


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def checkout_root() -> Path | None:
    """The git checkout when this module is running from it. Installed copies return None."""
    root = package_dir().parent
    marker = root / "skills" / "route-agent-work" / "scripts" / "route_judge.py"
    if (root / "pyproject.toml").is_file() and marker.is_file():
        return root
    return None


def skill_root() -> Path:
    """Directory that contains ``route-agent-work`` and the other skills as real files."""
    checkout = checkout_root()
    if checkout is not None:
        return checkout / "skills"
    bundled = package_dir() / "data" / "skills"
    marker = bundled / "route-agent-work" / "scripts" / "route_judge.py"
    if marker.is_file():
        return bundled
    raise FileNotFoundError(
        "the route-agent-work skill directory was not found next to this install "
        "or under agentcompass/data/skills"
    )


def setup_script(name: str) -> Path | None:
    checkout = checkout_root()
    if checkout is not None:
        path = checkout / "setup" / name
        if path.is_file():
            return path
    bundled = package_dir() / "data" / "setup" / name
    return bundled if bundled.is_file() else None
