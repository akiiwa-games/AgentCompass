"""Daily checks, deferred workers and notifications, with no real installers."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_updates as cli
import owned_process


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
    monkeypatch.setattr(cli, 'target_running', lambda target: 0)
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


def test_busy_worker_is_deferred_then_retried_without_touching_other_clis(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    cli.set_auto_update(True)
    monkeypatch.setattr(cli, 'target_running', lambda target: 2)
    installer = mock.Mock()
    monkeypatch.setattr(cli, 'update', installer)
    notify = mock.Mock()
    assert cli.maybe_trigger_auto_update(notify, now=200000, uptime=1000)
    installer.assert_not_called()
    assert cli._load_state()['deferred'] == ['codex']
    notify.assert_not_called()
    assert not cli.maybe_trigger_auto_update(notify, now=200059, uptime=1000)
    monkeypatch.setattr(cli.time, 'time', lambda: 200060.)
    monkeypatch.setattr(cli, 'target_running', lambda target: 0)
    def update(ident, *args, outcome):
        outcome.update(status='updated')
        return 'updated'
    monkeypatch.setattr(cli, 'update', update)
    assert cli.maybe_trigger_auto_update(notify, now=200060, uptime=1000)
    state = cli._load_state()
    assert state['last_auto_update_at'] == 200000  # a retry does not postpone the daily check
    assert state['deferred'] == []
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
    store = modules()[0].override_store
    with store.locked(tmp_path / 'cli-update-operation'):
        with mock.patch.object(cli, 'update') as install:
            result = cli.update_all()
    install.assert_not_called()
    assert not result['updated']
    assert 'last_auto_update_at' not in cli._load_state()


def test_node_script_process_is_detected_even_when_node_is_outside_package(tmp_path):
    package = tmp_path / 'pi'
    processes = [mock.Mock(info={'exe': str(tmp_path / 'node.exe'), 'cmdline': ['node', str(package / 'dist/cli.js')]}),
                 mock.Mock(info={'exe': str(tmp_path / 'node.exe'), 'cmdline': ['node', str(tmp_path / 'pi-sibling/cli.js')]})]
    with mock.patch('psutil.process_iter', return_value=processes):
        assert cli.running_instances(package) == 1


def test_native_cli_is_also_deferred_without_invoking_vendor_updater(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    native = cli.Target('grok', 'Grok', cli.Install(cli.BY_ID['grok'], tmp_path / 'grok.exe', 'native'), ['grok'])
    monkeypatch.setattr(cli, 'find', lambda *args: native)
    monkeypatch.setattr(cli, 'target_running', lambda target: 1)
    with mock.patch.object(cli, 'update_native') as installer:
        outcome = {}
        cli.update('grok', outcome=outcome)
    assert outcome['status'] == 'deferred'
    installer.assert_not_called()


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
    for var in ('PASEO_AGENT_ID', 'ROUTER_WORKER'):
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


def test_externally_terminated_install_stays_queued_and_retries_on_next_deferred_cycle(tmp_path, monkeypatch):
    env = _hermetic_npm_fixture(tmp_path, monkeypatch)
    prefix = env['prefix']
    pkg_dir = _write_npm_package(prefix, '@openai/codex', 'codex', '0.166.0', with_shim=True)
    shim = prefix / ('codex.cmd' if os.name == 'nt' else 'bin/codex')
    cli.set_auto_update(True)
    notify = mock.Mock()
    running = [mock.Mock(info={'exe': str(pkg_dir / 'vendor/codex.exe'), 'cmdline': [str(pkg_dir / 'vendor/codex.exe')]})]
    monkeypatch.setattr('psutil.process_iter', lambda *args, **kwargs: list(running))

    install_calls = []
    mode = {'install': 'killed'}

    def fake_run(argv, **kwargs):
        if 'view' in argv:
            return owned_process.Result(0, '0.167.1\n', '')
        if 'install' in argv:
            install_calls.append(list(argv))
            target_prefix = Path(argv[argv.index('--prefix') + 1])
            spec_arg = next(part for part in argv if part.endswith('@latest'))
            package = spec_arg[:-len('@latest')]
            if mode['install'] == 'killed':
                # Mimic logoff killing npm mid-install after extracting 0.167.1 and removing the old shim.
                shim.unlink(missing_ok=True)
                _write_npm_package(target_prefix, package, 'codex', '0.167.1', with_shim=False)
                return owned_process.Result(1073807364, '', '')
            _write_npm_package(target_prefix, package, 'codex', '0.167.1', with_shim=True)
            return owned_process.Result(0, 'changed 1 package', '')
        raise AssertionError(f'unexpected command: {argv}')

    monkeypatch.setattr(cli.owned_process, 'run', fake_run)

    # 1. Running session defers the daily update.
    monkeypatch.setattr(cli.time, 'time', lambda: 200000.0)
    assert cli.maybe_trigger_auto_update(notify, now=200000.0, uptime=1000.0)
    assert install_calls == []
    assert cli._load_state()['deferred'] == ['codex']
    notify.assert_not_called()

    # 2. Session exits; deferred retry starts npm, which is killed externally (rc=1073807364, no output).
    running.clear()
    monkeypatch.setattr(cli.time, 'time', lambda: 200060.0)
    assert cli.maybe_trigger_auto_update(notify, now=200060.0, uptime=1000.0)
    assert len(install_calls) == 1
    assert not shim.exists()
    assert json.loads((pkg_dir / 'package.json').read_text(encoding='utf-8'))['version'] == '0.167.1'
    state_after_kill = cli._load_state()
    assert state_after_kill['deferred'] == ['codex']
    assert state_after_kill['items']['codex']['kind'] == 'warn'
    assert '1073807364' in state_after_kill['items']['codex']['message']
    notify.assert_not_called()

    # 3. Respects the deferred retry cadence, then retries on the next cycle and restores the missing shim.
    assert not cli.maybe_trigger_auto_update(notify, now=200119.0, uptime=1000.0)
    mode['install'] = 'ok'
    monkeypatch.setattr(cli.time, 'time', lambda: 200120.0)
    assert cli.maybe_trigger_auto_update(notify, now=200120.0, uptime=1000.0)
    assert len(install_calls) == 2
    assert shim.is_file()
    final_state = cli._load_state()
    assert final_state['deferred'] == []
    assert final_state['last_auto_update_at'] == 200000.0
    assert final_state['items']['codex']['kind'] == 'ok'
    notify.assert_called_once()


def test_signal_terminated_install_stays_queued_until_a_stable_error_is_reported(tmp_path, monkeypatch):
    env = _hermetic_npm_fixture(tmp_path, monkeypatch)
    _write_npm_package(env['prefix'], '@openai/codex', 'codex', '0.166.0', with_shim=True)
    cli.set_auto_update(True)
    monkeypatch.setattr('psutil.process_iter', lambda *args, **kwargs: [])

    outcomes = [
        owned_process.Result(-15, 'extracting archive...', ''),
        owned_process.Result(1, '', 'npm error code EACCES: permission denied'),
    ]
    install_calls = []

    def fake_run(argv, **kwargs):
        if 'view' in argv:
            return owned_process.Result(0, '0.167.1\n', '')
        if 'install' in argv:
            install_calls.append(list(argv))
            return outcomes.pop(0)
        raise AssertionError(f'unexpected command: {argv}')

    monkeypatch.setattr(cli.owned_process, 'run', fake_run)

    monkeypatch.setattr(cli.time, 'time', lambda: 300000.0)
    assert cli.maybe_trigger_auto_update(now=300000.0, uptime=1000.0)
    assert len(install_calls) == 1
    assert cli._load_state()['deferred'] == ['codex']

    monkeypatch.setattr(cli.time, 'time', lambda: 300060.0)
    assert cli.maybe_trigger_auto_update(now=300060.0, uptime=1000.0)
    assert len(install_calls) == 2
    state = cli._load_state()
    assert state['deferred'] == []
    assert 'EACCES' in state['items']['codex']['message']

    monkeypatch.setattr(cli.time, 'time', lambda: 300120.0)
    assert not cli.maybe_trigger_auto_update(now=300120.0, uptime=1000.0)


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
