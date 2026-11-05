import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import install_cli


class ArtifactInstallationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        change = patch.object(install_cli, 'ROOT', self.root)
        change.start()
        self.addCleanup(change.stop)
        runtime = patch.dict(os.environ, {install_cli.grok_bot_paths.OVERRIDE: str(self.root / 'runtime')})
        runtime.start()
        self.addCleanup(runtime.stop)
        self.files = {'bin/gbot.mjs': b'synthetic entry', 'bin/gbot-flight.mjs': b'synthetic flight'}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for name, content in self.files.items():
                archive.writestr('grok-bot-cli-fixture/artifact/' + name, content)
        self.archive = self.root / 'upstream.zip'
        self.archive.write_bytes(buffer.getvalue())
        self.spec = {'commit': 'fixture', 'base_version': 'fixture',
                     'archive_sha256': install_cli.digest(buffer.getvalue()),
                     'artifact_sha256': {k: install_cli.digest(v) for k, v in self.files.items()},
                     'patched_sha256': {}}
        self.write_pin()

    def write_pin(self):
        (self.root / 'upstream.json').write_text(json.dumps(self.spec), encoding='utf-8')

    def test_exact_archive_installs_full_tree_and_check_is_read_only(self):
        with patch.object(install_cli.urllib.request, 'urlopen', side_effect=AssertionError('offline fixture')):
            install_cli.install(self.archive)
            before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            install_cli.check()
            after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            install_cli.install(self.archive)  # repair is repeatable
            self.assertEqual(list(self.root.glob('install-*')), [])

    def test_bad_archive_or_bad_patch_leaves_existing_installation_unchanged(self):
        import dependency_patch
        install_cli.install(self.archive)
        entry = self.root / 'runtime/bin/gbot.mjs'
        self.spec['patched_sha256'] = {'bin/gbot.mjs': install_cli.digest(b'expected patch')}
        self.write_pin()
        with patch.object(dependency_patch, 'patch_bundle', return_value=b'wrong patch'):
            with self.assertRaises(ValueError):
                install_cli.install(self.archive)
        self.assertEqual(entry.read_bytes(), self.files['bin/gbot.mjs'])
        self.archive.write_bytes(b'untrusted archive')
        with self.assertRaises(ValueError):
            install_cli.install(self.archive)
        self.assertEqual(entry.read_bytes(), self.files['bin/gbot.mjs'])

    def test_check_rejects_missing_or_tampered_flight_bundle(self):
        install_cli.install(self.archive)
        flight = self.root / 'runtime/bin/gbot-flight.mjs'
        flight.write_bytes(b'drift')
        with self.assertRaises(ValueError):
            install_cli.check()
        flight.unlink()
        with self.assertRaises(OSError):
            install_cli.check()


class RuntimeLocationTests(unittest.TestCase):
    """A wheel keeps the skill in site-packages; the CLI must live outside it to survive an upgrade."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.package = self.base / 'site-packages' / 'agentcompass' / 'data' / 'skills' / 'agent-compass' / 'grok-bot'
        self.env = {'ROUTER_STATE_DIR': str(self.base / 'state')}

    def test_install_goes_to_the_state_directory_not_the_skill(self):
        paths = install_cli.grok_bot_paths
        self.assertEqual(paths.install_dir(self.env), self.base / 'state' / 'grok-bot' / 'runtime')
        self.assertEqual(paths.runtime_dir(self.package, self.env), self.base / 'state' / 'grok-bot' / 'runtime')

    def test_a_cli_left_inside_the_skill_is_used_until_reinstalled(self):
        paths = install_cli.grok_bot_paths
        legacy = self.package / 'runtime' / 'bin' / 'gbot.mjs'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'old')
        self.assertEqual(paths.runtime_dir(self.package, self.env), self.package / 'runtime')
        current = self.base / 'state' / 'grok-bot' / 'runtime' / 'bin' / 'gbot.mjs'
        current.parent.mkdir(parents=True)
        current.write_bytes(b'new')
        self.assertEqual(paths.runtime_dir(self.package, self.env), current.parents[1])


if __name__ == '__main__':
    unittest.main()
