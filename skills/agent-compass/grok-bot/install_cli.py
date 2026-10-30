"""Install/check the pinned upstream artifact; never execute upstream install hooks or auth."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pin():
    return json.loads((ROOT / 'upstream.json').read_text(encoding='utf-8'))


def check(directory=None):
    directory = directory or ROOT / 'runtime'
    spec = pin()
    expected = {**spec['artifact_sha256'], **spec['patched_sha256']}
    for name, checksum in expected.items():
        if digest((directory / name).read_bytes()) != checksum:
            raise ValueError('Grok Bot CLI artifact differs from the pinned installation; use install/repair.')
    return f"grok-bot-cli {spec['base_version']} + upstream {spec['commit'][:12]} + scope binding verified"


def install(archive=None):
    from dependency_patch import patch_bundle

    spec = pin()
    if archive:
        data = Path(archive).read_bytes()
    else:
        with urllib.request.urlopen(spec['source'], timeout=60) as response:
            data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or digest(data) != spec['archive_sha256']:
        raise ValueError('Upstream archive checksum mismatch; installation was not changed.')
    # Extract only the exact artifact paths from the pinned archive, never archive-controlled destinations.
    with tempfile.TemporaryDirectory(prefix='install-', dir=ROOT) as temporary:
        temporary = Path(temporary)
        staged = temporary / 'runtime'
        staged.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            prefix = f"grok-bot-cli-{spec['commit']}/artifact/"
            for name, checksum in spec['artifact_sha256'].items():
                path = staged / name
                if not path.resolve().is_relative_to(staged.resolve()):
                    raise ValueError('Invalid pinned artifact path')
                content = bundle.read(prefix + name)
                if digest(content) != checksum:
                    raise ValueError('Upstream artifact checksum mismatch')
                if name in spec['patched_sha256']:
                    content = patch_bundle(name, content)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        message = check(staged)
        target, backup = ROOT / 'runtime', temporary / 'previous'
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise
    return message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--archive', type=Path, help='Offline copy of the exact pinned public archive')
    args = parser.parse_args()
    try:
        print(check() if args.check else install(args.archive))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        print('Grok Bot CLI install/check failed; verify the pinned source and use install/repair.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
