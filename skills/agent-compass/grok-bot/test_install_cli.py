import io
import json
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


if __name__ == '__main__':
    unittest.main()
