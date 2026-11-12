"""Daily checks, running sessions and notifications, with no real installers."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_updates as cli
import owned_process
import pytest


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target
        assert kwargs['daemon'] is False

    def start(self):
        self.target()


def configure(tmp_path, monkeypatch):
    monkeypatch.setenv('ROUTER_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(cli, '_auto_running', False)
    monkeypatch.setattr(cli, '_busy', {})
    monkeypatch.setattr(cli.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(cli.time, 'time', lambda: 200000.)
    target = cli.Target('codex', 'Codex CLI', cli.Install(cli.BY_ID['codex'], tmp_path / 'codex.cmd', 'npm'), ['codex'])
    monkeypatch.setattr(cli, 'targets', lambda *args: [target])
    return target


def test_automatic_is_opt_in_persisted_and_runs_at_most_daily(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    calls = []
    def update(ident, *args, outcome):
        calls.append(ident)
        outcome.update(status='current')
        return 'current'
    monkeypatch.setattr(cli, 'update', update)
    assert not cli.get_auto_update()
    assert not cli.maybe_trigger_auto_update(now=200000, uptime=1000)
    assert cli.set_auto_update(True)
    assert not cli.maybe_trigger_auto_update(now=200000, uptime=179)
    notify = mock.Mock()
    assert cli.maybe_trigger_auto_update(notify, now=200000, uptime=180)
    assert calls == ['codex']
    assert not cli.maybe_trigger_auto_update(now=200000 + 86400 - 1, uptime=1000)
    assert cli.maybe_trigger_auto_update(now=200000 + 86400, uptime=1000)
    assert calls == ['codex', 'codex']
    notify.assert_not_called()  # no update never becomes a popup
    cli.set_auto_update(False)
    assert not cli.get_auto_update()


def test_daily_update_runs_without_inspecting_sessions_and_has_no_retry_queue(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    cli.set_auto_update(True)
    calls = []
    def update(ident, *args, outcome):
        calls.append(ident)
        outcome.update(status='updated')
        return 'updated'
    monkeypatch.setattr(cli, 'update', update)
    monkeypatch.setattr('psutil.process_iter', mock.Mock(side_effect=AssertionError('session inspection')))
    notify = mock.Mock()
    assert cli.maybe_trigger_auto_update(notify, now=200000, uptime=1000)
    assert calls == ['codex']
    assert 'deferred' not in cli._load_state()
    assert not cli.maybe_trigger_auto_update(notify, now=200060, uptime=1000)
    notify.assert_called_once()


def test_active_manual_operation_or_auto_worker_prevents_duplicate_start(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    cli.set_auto_update(True)
    monkeypatch.setattr(cli, '_busy', {'codex': 'update'})
    assert not cli.maybe_trigger_auto_update(now=200000, uptime=1000)
    monkeypatch.setattr(cli, '_busy', {})
    monkeypatch.setattr(cli, '_auto_running', True)
    assert not cli.maybe_trigger_auto_update(now=200000, uptime=1000)


def test_a_batch_in_another_process_defers_this_batch(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    from router_priority import modules
    modules()
    import override_store as store
    with store.locked(tmp_path / 'cli-update-operation'):
        with mock.patch.object(cli, 'update') as install:
            result = cli.update_all()
    install.assert_not_called()
    assert not result['updated']
    assert 'last_auto_update_at' not in cli._load_state()


@pytest.mark.parametrize('ident', ['claude', 'opencode', 'agy', 'grok'])
def test_native_updaters_run_without_inspecting_sessions(tmp_path, monkeypatch, ident):
    configure(tmp_path, monkeypatch)
    native = cli.Target(ident, ident, cli.Install(cli.BY_ID[ident], tmp_path / f'{ident}.exe', 'native'), [ident])
    monkeypatch.setattr(cli, 'find', lambda *args: native)
    monkeypatch.setattr(cli, 'resolve_command', lambda *args, **kwargs: native.install.path)
    monkeypatch.setattr('psutil.process_iter', mock.Mock(side_effect=AssertionError('session inspection')))
    with mock.patch.object(cli, 'update_native', return_value=('updated', 'done')) as installer:
        outcome = {}
        cli.update(ident, outcome=outcome)
    assert outcome['status'] == 'updated'
    installer.assert_called_once_with(native)


def test_snapshot_restores_cached_remote_version_without_a_network_probe(tmp_path, monkeypatch):
    target = configure(tmp_path, monkeypatch)
    cli._change_state(lambda state: state.update(items={'codex': {'latest': '2.0.0'}}))
    monkeypatch.setattr(cli, '_latest', {})
    monkeypatch.setattr(cli, 'local_version', lambda *args: '1.0.0')
    assert cli.describe(target, probe=False)['status'] == 'outdated'


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)


def _write_shim(prefix: Path, package: str, command: str) -> Path:
    if os.name == 'nt':
        shim = prefix / f'{command}.cmd'
        target = '\\'.join(package.split('/'))
        _write_text(shim, f'@ECHO off\n"%dp0%\\node_modules\\{target}\\bin\\{command}.js" %*\n')
        return shim
    shim = prefix / 'bin' / command
    _write_text(shim, '#!/bin/sh\nexit 0\n')
    shim.chmod(0o755)
    return shim


def _write_npm_package(prefix: Path, package: str, command: str, version: str, *, with_shim: bool = True) -> Path:
    base = prefix / ('node_modules' if os.name == 'nt' else 'lib/node_modules')
    pkg_dir = base.joinpath(*package.split('/'))
    _write_text(pkg_dir / 'package.json', json.dumps({'name': package, 'version': version}))
    if with_shim:
        _write_shim(prefix, package, command)
    return pkg_dir


def _hermetic_npm_fixture(tmp_path: Path, monkeypatch):
    for var in ('PASEO_AGENT_ID', 'AGENTCOMPASS_WORKER'):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / 'home'
    local = tmp_path / 'local'
    roaming = tmp_path / 'roaming'
    state = tmp_path / 'state'
    prefix = roaming / 'npm'
    node_bin = tmp_path / 'node-bin'
    for directory in (home, local, roaming, state, prefix, node_bin):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        _write_text(node_bin / 'npm.cmd', '@ECHO off\n')
        (node_bin / 'node.exe').write_bytes(b'MZ')
    else:
        npm_bin = node_bin / 'npm'
        _write_text(npm_bin, '#!/bin/sh\n')
        npm_bin.chmod(0o755)
        node_exe = node_bin / 'node'
        _write_text(node_exe, '#!/bin/sh\n')
        node_exe.chmod(0o755)
    _write_text(node_bin / 'node_modules/npm/bin/npm-cli.js', '')
    prefix_path_entry = str(prefix if os.name == 'nt' else prefix / 'bin')
    path_value = os.pathsep.join([prefix_path_entry, str(node_bin)])
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('APPDATA', str(roaming))
    monkeypatch.setenv('PASEO_HOME', str(home / '.paseo'))
    monkeypatch.setenv('ROUTER_STATE_DIR', str(state))
    monkeypatch.setenv('PATH', path_value)
    if os.name == 'nt':
        monkeypatch.setenv('PATHEXT', '.COM;.EXE;.BAT;.CMD')
    monkeypatch.setattr(cli, '_auto_running', False)
    monkeypatch.setattr(cli, '_busy', {})
    monkeypatch.setattr(cli, '_latest', {})
    cli._clear_which_cache()
    monkeypatch.setattr(cli.threading, 'Thread', ImmediateThread)
    return {'home': home, 'local': local, 'roaming': roaming, 'state': state, 'prefix': prefix, 'node_bin': node_bin}


@pytest.mark.parametrize('result', [owned_process.Result(1073807364, '', ''),
                                  owned_process.Result(-15, 'extracting', ''),
                                  owned_process.Result(1, '', 'EACCES')])
def test_failed_install_is_reported_and_does_not_schedule_a_session_retry(tmp_path, monkeypatch, result):
    env = _hermetic_npm_fixture(tmp_path, monkeypatch)
    _write_npm_package(env['prefix'], '@openai/codex', 'codex', '0.166.0')
    calls = []
    def fake_run(argv, **kwargs):
        if 'view' in argv:
            return owned_process.Result(0, '0.167.1', '')
        if 'install' in argv:
            calls.append(argv)
            return result
        raise AssertionError(argv)
    monkeypatch.setattr(cli.owned_process, 'run', fake_run)
    cli.set_auto_update(True)
    monkeypatch.setattr(cli.time, 'time', lambda: 300000.)
    batch = cli.update_all(auto=True)
    assert batch['failed'] == ['codex']
    assert 'deferred' not in batch
    assert 'deferred' not in cli._load_state()
    assert len(calls) == 1
    assert not cli.maybe_trigger_auto_update(now=300060, uptime=1000)


def test_package_present_with_missing_command_triggers_reinstall_at_latest_version(tmp_path, monkeypatch):
    env = _hermetic_npm_fixture(tmp_path, monkeypatch)
    prefix = env['prefix']
    _write_npm_package(prefix, '@openai/codex', 'codex', '0.167.1', with_shim=False)
    shim = prefix / ('codex.cmd' if os.name == 'nt' else 'bin/codex')
    monkeypatch.setattr('psutil.process_iter', lambda *args, **kwargs: [])

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if 'view' in argv:
            return owned_process.Result(0, '0.167.1\n', '')
        if 'install' in argv:
            target_prefix = Path(argv[argv.index('--prefix') + 1])
            _write_npm_package(target_prefix, '@openai/codex', 'codex', '0.167.1', with_shim=True)
            return owned_process.Result(0, 'changed 1 package', '')
        raise AssertionError(f'unexpected command: {argv}')

    monkeypatch.setattr(cli.owned_process, 'run', fake_run)

    before_row = next(row for row in cli.snapshot() if row['id'] == 'codex')
    assert calls == []
    assert before_row['source'] == 'npm'
    assert before_row['version'] == '0.167.1'
    assert before_row['path'] == ''
    assert before_row['status'] == 'outdated'
    assert before_row['can_update'] is True

    result = cli.update_all()
    install_calls = [argv for argv in calls if 'install' in argv]
    assert len(install_calls) == 1
    assert install_calls[0][install_calls[0].index('--prefix') + 1] == str(prefix)
    assert result['updated'] == ['codex']
    assert result['current'] == []
    assert result['failed'] == []
    assert shim.is_file()

    after_row = next(row for row in cli.snapshot() if row['id'] == 'codex')
    assert after_row['status'] == 'current'
    assert Path(after_row['path']) == shim

    second_msg = cli.update('codex')
    assert '已是最新' in second_msg
    assert len([argv for argv in calls if 'install' in argv]) == 1


def test_configured_provider_with_missing_command_reinstalls_into_launcher_prefix(tmp_path, monkeypatch):
    env = _hermetic_npm_fixture(tmp_path, monkeypatch)
    home = env['home']
    node_bin = env['node_bin']
    decoy_prefix = tmp_path / 'decoy-npm'
    provider_prefix = tmp_path / 'provider-npm'
    explicit_prefix = tmp_path / 'explicit-claude-npm'
    for directory in (decoy_prefix, provider_prefix, explicit_prefix):
        directory.mkdir(parents=True, exist_ok=True)

    _write_npm_package(decoy_prefix, '@openai/codex', 'codex', '0.167.1', with_shim=True)
    _write_npm_package(provider_prefix, '@openai/codex', 'codex', '0.167.1', with_shim=False)
    _write_npm_package(explicit_prefix, '@anthropic-ai/claude-code', 'claude', '2.1.0', with_shim=False)

    decoy_entry = str(decoy_prefix if os.name == 'nt' else decoy_prefix / 'bin')
    provider_entry = str(provider_prefix if os.name == 'nt' else provider_prefix / 'bin')
    explicit_shim = explicit_prefix / ('claude.cmd' if os.name == 'nt' else 'bin/claude')
    provider_shim = provider_prefix / ('codex.cmd' if os.name == 'nt' else 'bin/codex')

    monkeypatch.setenv('PATH', os.pathsep.join([decoy_entry, str(node_bin)]))
    _write_text(home / '.paseo/config.json', json.dumps({
        'agents': {
            'providers': {
                'codex-work': {
                    'extends': 'codex',
                    'enabled': True,
                    'command': ['codex'],
                    'env': {'PATH': os.pathsep.join([provider_entry, str(node_bin)])},
                },
                'claude-lab': {
                    'extends': 'claude',
                    'enabled': True,
                    'command': [str(explicit_shim)],
                },
            }
        }
    }))
    monkeypatch.setattr('psutil.process_iter', lambda *args, **kwargs: [])

    installed_prefixes = []

    def fake_run(argv, **kwargs):
        if 'view' in argv:
            pkg = argv[argv.index('view') + 1]
            return owned_process.Result(0, '0.167.1\n' if pkg == '@openai/codex' else '2.1.0\n', '')
        if 'install' in argv:
            target_prefix = Path(argv[argv.index('--prefix') + 1])
            spec_arg = next(part for part in argv if part.endswith('@latest'))
            package = spec_arg[:-len('@latest')]
            installed_prefixes.append((package, target_prefix))
            cmd = 'codex' if package == '@openai/codex' else 'claude'
            ver = '0.167.1' if package == '@openai/codex' else '2.1.0'
            _write_npm_package(target_prefix, package, cmd, ver, with_shim=True)
            return owned_process.Result(0, 'changed 1 package', '')
        raise AssertionError(f'unexpected command: {argv}')

    monkeypatch.setattr(cli.owned_process, 'run', fake_run)

    outcome_codex = {}
    cli.update('codex#codex-work', outcome=outcome_codex)
    assert outcome_codex['status'] == 'updated'
    assert provider_shim.is_file()

    outcome_claude = {}
    cli.update('claude#claude-lab', outcome=outcome_claude)
    assert outcome_claude['status'] == 'updated'
    assert explicit_shim.is_file()

    assert installed_prefixes == [
        ('@openai/codex', provider_prefix),
        ('@anthropic-ai/claude-code', explicit_prefix),
    ]
