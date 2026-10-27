import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import runtime_paths as rp


class RuntimePathsTests(unittest.TestCase):
    def test_uv_uses_active_python_user_scripts_without_a_fixed_username_or_version(self):
        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / 'Python399' / 'Scripts'
            scripts.mkdir(parents=True)
            uv = scripts / 'uv.exe'
            uv.touch()
            with patch.dict(rp.os.environ, {'USERPROFILE': td}, clear=True), patch.object(rp.shutil, 'which', return_value=None), \
                    patch.object(rp.sysconfig, 'get_path', return_value=str(scripts)):
                self.assertEqual(rp.uv_path(), uv)

    def test_cloudflared_uses_workspace_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            exe = root / 'tools/cloudflared/cloudflared.exe'
            exe.parent.mkdir(parents=True)
            exe.touch()
            with patch.dict(rp.os.environ, {}, clear=True), patch.object(rp.shutil, 'which', return_value=None), \
                    patch.object(rp, 'ROOT', root):
                self.assertEqual(rp.cloudflared_path(), exe)

    def test_explicit_missing_override_reports_it_instead_of_using_another_install(self):
        with patch.dict(rp.os.environ, {'ROUTE_MANAGER_UV': 'Z:/absent-uv.exe'}):
            with self.assertRaises(FileNotFoundError):
                rp.uv_path()


if __name__ == '__main__':
    unittest.main()
