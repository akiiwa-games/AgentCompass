import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

spec = importlib.util.spec_from_file_location('claude2_config', Path(__file__).resolve().parents[1] / 'setup' / 'claude2_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class Claude2ConfigTests(unittest.TestCase):
    def test_isolation_and_existing_settings_are_preserved(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude-2').mkdir()
            (home / '.claude-2' / '.credentials.json').write_text('secret-placeholder', encoding='utf-8')
            p = home / '.paseo' / 'config.json'
            p.parent.mkdir()
            p.write_text(json.dumps({'agents': {'providers': {'codex-2': {'enabled': False}}}, 'daemon': {'listen': 'localhost:1'}}), encoding='utf-8')
            f = home / '.usage-float' / 'config.json'
            f.parent.mkdir()
            f.write_text(json.dumps({'providers': ['claude:7d'], 'provider_order': ['claude:7d'],
                                     'disabled_providers': ['claude:5h'], 'font_size': 11}), encoding='utf-8')
            config.configure(home)
            first = json.loads(p.read_text(encoding='utf-8'))
            self.assertFalse(first['agents']['providers']['codex-2']['enabled'])
            self.assertEqual(first['daemon'], {'listen': 'localhost:1'})
            second_env = first['agents']['providers']['claude-2']['env']
            self.assertEqual(second_env['CLAUDE_CONFIG_DIR'], str(home / '.claude-2'))
            self.assertEqual(second_env['CLAUDE_SECURESTORAGE_CONFIG_DIR'], second_env['CLAUDE_CONFIG_DIR'])
            floating = json.loads(f.read_text(encoding='utf-8'))
            self.assertEqual(floating['providers'], ['claude:7d', *config.SECOND_ROWS])
            self.assertEqual(floating['disabled_providers'], ['claude:5h'])
            self.assertEqual(floating['font_size'], 11)
            config.configure(home)
            self.assertEqual(json.loads(p.read_text(encoding='utf-8')), first)
            self.assertEqual((home / '.claude-2' / '.credentials.json').read_text(), 'secret-placeholder')
            self.assertTrue(list((home / '.agent-compass' / 'backups').glob('claude-2-*')))

    def test_repairs_empty_secure_storage_without_changing_other_providers(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude-2').mkdir()
            (home / '.claude-2' / '.credentials.json').touch()
            p = home / '.paseo' / 'config.json'
            p.parent.mkdir()
            legacy = config.provider_config(home)
            legacy['env']['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = ''
            p.write_text(json.dumps({'agents': {'providers': {
                'claude-2': legacy, 'claude': {'enabled': True}}}}), encoding='utf-8')
            config.configure(home)
            providers = json.loads(p.read_text(encoding='utf-8'))['agents']['providers']
            self.assertEqual(providers['claude-2'], config.provider_config(home))
            self.assertEqual(providers['claude'], {'enabled': True})

    def test_any_account_name_and_home_register_the_same_way(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            account_home = home / 'profiles' / 'work-claude'
            account_home.mkdir(parents=True)
            (account_home / '.credentials.json').touch()
            config.configure(home, 'team-backup', account_home)
            provider = json.loads((home / '.paseo' / 'config.json').read_text(encoding='utf-8'))['agents']['providers']['team-backup']
            self.assertEqual((provider['label'], provider['env']['CLAUDE_CONFIG_DIR']), ('Claude Team Backup', str(account_home)))
            floating = json.loads((home / '.usage-float' / 'config.json').read_text(encoding='utf-8'))
            self.assertEqual(floating['providers'], ['team-backup:7d', 'team-backup:5h'])
            sources = json.loads((home / '.agent-compass' / 'capacity-sources.json').read_text(encoding='utf-8'))
            self.assertEqual(sources['team-backup']['roots'], [str(account_home / 'projects')])
            with self.assertRaises(ValueError):
                config.configure(home, 'Work Account', account_home)

    def test_requires_a_separate_login(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                config.configure(Path(directory))

    def test_conflicting_provider_is_not_replaced(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude-2').mkdir()
            (home / '.claude-2' / '.credentials.json').touch()
            p = home / '.paseo' / 'config.json'
            p.parent.mkdir()
            original = json.dumps({'agents': {'providers': {'claude-2': {'enabled': False}}}})
            p.write_text(original, encoding='utf-8')
            with self.assertRaises(RuntimeError):
                config.configure(home)
            self.assertEqual(p.read_text(encoding='utf-8'), original)


if __name__ == '__main__':
    unittest.main()
