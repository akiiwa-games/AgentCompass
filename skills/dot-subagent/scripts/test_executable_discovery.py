import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import github_send
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tray'))
import gateways
import runtime_paths

class DiscoveryTests(unittest.TestCase):
    cases = ((github_send, github_send.find_openssl, 'AGENTCOMPASS_OPENSSL'),
             (runtime_paths, gateways.chrome_path, 'MUSE2API_CHROMIUM_PATH'))

    def test_override_precedes_path(self):
        with tempfile.TemporaryDirectory() as temp:
            tool = Path(temp) / 'tool'
            tool.touch()
            for module, finder, variable in self.cases:
                with self.subTest(variable=variable), patch.dict(os.environ, {'USERPROFILE': str(Path.home()), variable: str(tool)}, clear=True), patch.object(module.shutil, 'which', return_value='/path/tool') as which:
                    self.assertEqual(str(finder()), str(tool))
                    which.assert_not_called()

    def test_bad_override_does_not_fall_back(self):
        for module, finder, variable in self.cases:
            with self.subTest(variable=variable), patch.dict(os.environ, {'USERPROFILE': str(Path.home()), variable: '/missing/tool'}, clear=True), patch.object(Path, 'is_file', return_value=False), patch.object(module.shutil, 'which') as which:
                with self.assertRaisesRegex(FileNotFoundError, variable): finder()
                which.assert_not_called()

    def test_path_precedes_candidates(self):
        for module, finder, variable in self.cases:
            with self.subTest(variable=variable), patch.dict(os.environ, {'USERPROFILE': str(Path.home())}, clear=True), patch.object(module.shutil, 'which', return_value='/path/tool'), patch.object(Path, 'is_file', side_effect=AssertionError('candidate checked before PATH')):
                self.assertEqual(Path(finder()), Path('/path/tool'))

    def test_common_install_candidate(self):
        for module, finder, variable in self.cases:
            with self.subTest(variable=variable), patch.dict(os.environ, {'USERPROFILE': str(Path.home())}, clear=True), patch.object(module.shutil, 'which', return_value=None), patch.object(Path, 'is_file', return_value=True):
                self.assertIn('Program Files', str(finder()))

    def test_missing_tool_has_actionable_error(self):
        for module, finder, variable in self.cases:
            with self.subTest(variable=variable), patch.dict(os.environ, {'USERPROFILE': str(Path.home())}, clear=True), patch.object(module.shutil, 'which', return_value=None), patch.object(Path, 'is_file', return_value=False):
                with self.assertRaisesRegex(FileNotFoundError, variable): finder()

if __name__ == '__main__': unittest.main()
