#!/usr/bin/env python3
"""Report routing-document syntax and local reference errors, not routing quality."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


def check_documents(skills_root: Path) -> tuple[list[str], int]:
    failures: list[str] = []
    checked: set[Path] = set()
    pending = [skills_root / name / 'SKILL.md' for name in
               ('agent-compass', 'chatgpt-pro-consult', 'dot-subagent', 'gemini-web')]
    while pending:
        path = pending.pop().resolve()
        if path in checked:
            continue
        checked.add(path)
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            failures.append(f'{path}: {exc}')
            continue
        if path.name == 'SKILL.md':
            match = re.match(r'\A---\s*\n(.*?)\n---(?:\s*\n|\Z)', text, re.S)
            try:
                metadata = yaml.safe_load(match.group(1)) if match else None
                if not isinstance(metadata, dict):
                    raise ValueError('missing YAML frontmatter mapping')
                for field in ('name', 'description'):
                    if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                        raise ValueError(f'missing nonempty {field}')
            except (yaml.YAMLError, ValueError) as exc:
                failures.append(f'{path}: {exc}')
        # These router references use ordinary inline Markdown links. Code blocks are examples.
        prose = re.sub(r'(?ms)^```[^\n]*\n.*?^```[^\n]*(?:\n|$)', '', text)
        for match in re.finditer(r'\[[^\]\n]+\]\(([^)\n]+)\)', prose):
            target = match.group(1).strip().strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path or target.startswith('#'):
                continue
            linked = (path.parent / unquote(parsed.path)).resolve()
            if not linked.is_file():
                failures.append(f'{path}: missing local reference {target}')
            elif linked.suffix.lower() == '.md' and linked.is_relative_to(skills_root):
                pending.append(linked)
    return failures, len(checked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    failures, count = check_documents(args.skills_root.resolve())
    for failure in failures:
        print(f'ERROR: {failure}')
    print(f'Checked {count} routing documents for readable structure and local references. '
          'Routing choices, authority, and analytical quality require contextual review.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
