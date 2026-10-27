"""Expose the installed desktop CUA runtime to another Codex host.

Read the app-generated manifest on every launch so desktop upgrades and pipe
changes do not leave copied runtime paths in that host's config.
This launches the existing MCP server; it does not implement browser control.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--desktop-home', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    root = args.desktop_home / 'plugins/cache/openai-bundled/unified-computer-use'
    candidates = list(root.glob('*/.mcp.json'))
    if not candidates:
        raise RuntimeError('Desktop CUA manifest is missing; open/update Codex desktop.')
    manifest = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    server = json.loads(manifest.read_text(encoding='utf-8'))['mcpServers']['cua_repl']
    command = [server['command'], *server.get('args', [])]
    for item in command[:2]:
        if not Path(item).is_file():
            raise RuntimeError('Desktop CUA executable or entrypoint is missing.')
    if args.check:
        print(json.dumps({'manifest': str(manifest), 'runtime_exists': True,
                          'surfaces': server.get('env', {}).get('CUA_REPL_ENABLED_SURFACES') }))
        return 0
    env = os.environ.copy()
    env.update(server.get('env', {}))
    child = subprocess.Popen(command, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return child.wait()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(f'CUA launcher: {exc}', file=sys.stderr)
        sys.exit(1)
