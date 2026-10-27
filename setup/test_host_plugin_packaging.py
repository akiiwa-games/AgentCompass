"""Isolated checks for plugin delivery and per-host skill paths; no host sync runs."""
import ast
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SETUP = Path(__file__).resolve().parent


class HostPluginPackagingTests(unittest.TestCase):
    def test_skill_roots_are_absolute_for_target_home(self):
        source = ast.parse((SETUP / 'sync_host_globals.py').read_text(encoding='utf-8'))
        assignment = next(node for node in ast.walk(source) if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'gemini_skills_cfg' for t in node.targets))
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            config = eval(compile(ast.Expression(assignment.value), '<skill-paths>', 'eval'), {'HOME': home})
            self.assertEqual([entry['path'] for entry in config['entries']],
                             [(home / name / 'skills').as_posix() for name in ('.codex', '.agents', '.claude')])
            self.assertTrue(all(Path(entry['path']).is_absolute() for entry in config['entries']))

    @unittest.skipUnless((SETUP / 'host_sync/build_bundle.py').is_file(),
                         'Optional private host-handover tooling is not part of the public source package')
    def test_handover_includes_fast_source_without_dependencies(self):
        spec = importlib.util.spec_from_file_location('bundle_fixture', SETUP / 'host_sync/build_bundle.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = root / 'paseo-pi-fast'
            plugin.mkdir()
            (plugin / 'index.client.tsx').write_text('// fixture')
            (plugin / 'node_modules').mkdir()
            (plugin / 'node_modules/secret-fixture').write_text('must not ship')
            with patch.multiple(module, ROOT=root, HOME=root / 'home', OUT=root / 'out'), \
                    patch.object(module, 'git', return_value=''), redirect_stdout(io.StringIO()):
                module.main()
            archive = next((root / 'out').glob('*.zip'))
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn('workspace/paseo-pi-fast/index.client.tsx', bundle.namelist())
                self.assertFalse(any('/node_modules/' in name for name in bundle.namelist()))
                manifest = json.loads(bundle.read('manifest.json'))
                self.assertIn('workspace/paseo-pi-fast/index.client.tsx', [entry['path'] for entry in manifest['files']])


if __name__ == '__main__':
    unittest.main()
