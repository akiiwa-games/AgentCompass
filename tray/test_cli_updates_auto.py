"""Daily checks, deferred workers and notifications, with no real installers."""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_updates as cli


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
