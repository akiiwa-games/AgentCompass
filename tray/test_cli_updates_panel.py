"""The three model-settings pages, through real Tk controls and mocked installers."""
import gc
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_updates
import i18n
import plugins
import router_data
import router_window
import tk_test_support


def row(harness='codex', account='codex', enabled=True, partial=False):
    return {'account': account, 'account_label': router_data.account_label(account), 'harness': harness,
            'harness_label': router_data.HARNESS_LABELS.get(harness, harness), 'enabled': enabled,
            'host_enabled': enabled, 'verified': False, 'badge': '未发现', 'kind': 'idle',
            'models': [{'model': 'gpt-6.1-sol', 'model_label': 'Sol 6.1', 'enabled': not partial,
                        'partial': partial, 'bundle_ids': ['sol_high', 'sol_medium']}]}


CLI_ROWS = [{'id': 'codex', 'path': 'codex.cmd', 'source': 'npm', 'version': '0.165',
             'status': 'outdated', 'providers': ['codex']}]
RESULT = {'summary': 'CLI：已更新 1 · 已是最新 0 · 延后 0 · 失败 0', 'updated': ['codex'], 'failed': []}


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared_root = tk_test_support.shared_tk_root()

    @classmethod
    def tearDownClass(cls):
        tk_test_support.reset_tk_root(cls.shared_root)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for patcher in (
            mock.patch.object(router_data, 'STATE_DIR', self.directory),
            mock.patch.object(cli_updates, 'state_dir', return_value=self.directory),
            mock.patch.object(cli_updates, 'snapshot', return_value=CLI_ROWS),
            mock.patch.object(router_data, 'harness_model_rows', return_value=[row(), row('pi')]),
            mock.patch.object(router_data, 'refresh_model_catalog', return_value=[row(), row('pi')]),
            mock.patch.object(router_data, 'overrides', return_value=[]),
            mock.patch.object(router_data, 'boost_targets', return_value=[]),
            mock.patch.object(router_window.theme, 'set_window_icon'),
            mock.patch.object(router_window, 'log_error'),
            mock.patch.object(plugins, 'load', return_value=plugins.defaults()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        i18n.set_language('zh')
        self.root = self.shared_root
        self.root.withdraw()
        self.addCleanup(tk_test_support.reset_tk_root, self.root, self)

    def open(self, real_worker=False):
        patcher = mock.patch.object(router_window.Panel, 'prefill')
        patcher.start()
        self.addCleanup(patcher.stop)
        if not real_worker:
            patcher = mock.patch.object(router_window.Panel, 'run')
            patcher.start()
            self.addCleanup(patcher.stop)
        panel = router_window.Panel(self.root, 'policy')
        self.addCleanup(lambda: self.root.after_cancel(panel._poll_id))
        self.panel = panel
        return panel

    def test_three_tabs_and_legacy_links_redirect_to_the_new_pages(self):
        panel = self.open()
        self.assertEqual([key for key, _ in router_window.SETTINGS], ['models', 'strategy', 'system'])
        self.assertEqual(panel.current_setting, 'models')
        for key in ('accounts', 'boost', 'cli'):
            panel.show_setting(key)
            self.assertEqual(panel.current_setting, 'models')
        for key in ('tuning', 'dna'):
            panel.show_setting(key)
            self.assertEqual(panel.current_setting, 'strategy')
        self.assertEqual(set(panel.setting_frames), {'models', 'strategy', 'system'})

    def test_home_shows_cli_summary_and_daily_toggle_without_installing_on_entry(self):
        with mock.patch.object(cli_updates, 'update_all') as installer:
            panel = self.open()
            panel.cli_loaded(CLI_ROWS)
            self.assertEqual(panel.cli_status.get(), 'CLI：1 已安装 · 1 可更新')
            self.assertEqual(panel.cli_auto_check.cget('text'), '自动更新（每天）')
            panel.show_setting('system')
            panel.show_setting('models')
            installer.assert_not_called()

    def test_english_home_and_language_switch_preserve_the_selected_new_tab(self):
        panel = self.open()
        panel.change_language('en')
        self.assertEqual(panel.nav['policy'][2].cget('text'), 'Model Settings')
        self.assertEqual(panel.setting_switch.buttons['models'].cget('text'), 'Models & Boost')
        self.assertEqual(panel.setting_switch.buttons['strategy'].cget('text'), 'Scoring & DNA')
        self.assertEqual(panel.cli_auto_check.cget('text'), 'Auto-update daily')
        panel.cli_loaded(CLI_ROWS)
        self.assertEqual(panel.cli_status.get(), 'CLIs: 1 installed · 1 updates')
        self.assertEqual(panel.current_setting, 'models')

    def test_harness_and_account_rows_keep_model_partial_state_and_readiness_separate(self):
        panel = self.open()
        panel.models_rows_loaded([row(partial=True), row('pi', enabled=False), row(account='codex-2')])
        self.assertEqual(len(panel.model_widgets), 3)
        first = panel.model_widgets[0]
        self.assertTrue(first['chip_labels'][0].cget('text').startswith('◐ '))
        self.assertTrue(first['on'].get())  # verified=False is not the checkbox value
        panel.model_widgets[1]['dropdown'].invoke()
        self.assertTrue(panel.model_menu_items[0][2].instate(['disabled']))
        self.assertEqual(first['version'].cget('text'), '')
        panel.cli_loaded(CLI_ROWS)
        self.assertEqual(first['version'].cget('text'), 'v0.165')
        with mock.patch.object(router_data, 'toggle_harness_model', return_value=True) as toggle:
            first['dropdown'].invoke()
            panel.run.reset_mock()
            item = panel.model_menu_items[0][2]
            if hasattr(item, 'invoke'):
                item.invoke()
            else:
                panel.model_menu.update_idletasks()
                item.event_generate('<Button-1>')
            panel.run.call_args.args[0]()
            toggle.assert_called_once_with('codex', 'codex', 'gpt-6.1-sol', True)

    def test_harness_toggle_does_not_call_the_old_account_or_bundle_toggle(self):
        panel = self.open()
        panel.models_rows_loaded([row('pi')])
        with mock.patch.object(router_data, 'toggle_harness') as toggle:
            panel.run.reset_mock()
            panel.model_widgets[0]['check'].invoke()
            panel.run.call_args.args[0]()
            toggle.assert_called_once_with('pi', 'codex', False)

    def test_update_button_is_deduplicated_and_runs_the_batch_off_the_ui_thread(self):
        panel = self.open()
        panel.run.reset_mock()
        with mock.patch.object(cli_updates, 'update_all', return_value=RESULT) as installer:
            panel.cli_update_button.invoke()
            panel.cli_update_button.invoke()
            self.assertEqual(panel.run.call_count, 1)
            self.assertTrue(panel.cli_update_button.instate(['disabled']))
            work, done = panel.run.call_args.args[:2]
            self.assertTrue(panel.run.call_args.kwargs['long'])
            self.assertEqual(work(), RESULT)
            installer.assert_called_once()
            done(RESULT)
            self.assertFalse(panel.cli_update_button.instate(['disabled']))
            self.assertEqual(panel.cli_status.get(), RESULT['summary'])

    def test_failed_batch_restores_the_update_button_and_reports_failure(self):
        panel = self.open()
        panel.run.reset_mock()
        with mock.patch.object(cli_updates, 'update_all', side_effect=OSError('offline')):
            panel.update_all_cli()
            work, done = panel.run.call_args.args[:2]
            done(work())
        self.assertFalse(panel.cli_update_button.instate(['disabled']))
        self.assertIn('offline', panel.cli_status.get())

    def test_daily_checkbox_persists_the_actual_selected_value(self):
        panel = self.open()
        panel.run.reset_mock()
        with mock.patch.object(cli_updates, 'set_auto_update', return_value=True) as save:
            panel.cli_auto_check.invoke()
            work, done = panel.run.call_args.args[:2]
            done(work())
            save.assert_called_once_with(True)
            self.assertEqual(panel.status.get(), '已开启每日 CLI 自动更新')

    def test_strategy_has_one_save_and_presets_only_load_the_form(self):
        panel = self.open()
        panel.show_setting('strategy')
        panel.tuning_loaded(router_data.TUNING_DEFAULTS)
        with mock.patch.object(router_data, 'save_tuning') as save:
            panel.preset_buttons['省额度'].invoke()
            save.assert_not_called()
            self.assertEqual(float(panel.tuning_vars['safety_margin_pct'].get()), 12)
        panel.models = {'gpt-6.1-sol': {'subjective': {'divergence': .5, 'global_rigor': .6, 'module_precision': .9}}}
        panel.fill_dna_models()
        panel.run.reset_mock()
        with mock.patch.object(router_data, 'save_strategy', return_value='saved') as save:
            panel.strategy_save.invoke()
            work, done = panel.run.call_args.args[:2]
            done(work())
            self.assertEqual(save.call_args.args[1], 'gpt-6.1-sol')
            self.assertEqual(panel.status.get(), 'saved')

    def test_strategy_value_table_edits_rows_and_keeps_one_save(self):
        panel = self.open()
        panel.show_setting('strategy')
        self.assertFalse(panel._value_ready)
        self.assertEqual(panel.strategy_save.cget('text'), '保存策略与画像')
        panel.value_loaded((
            {'default': 1.0, 'rules': [
                {'harness': 'antigravity', 'model_prefix': 'claude', 'factor': 0.9, 'basis': 'keep'}]},
            {'score_per_displaced_usd': 0.012, 'score_per_displaced_worker_hour': 0.06}))
        self.assertEqual(panel.collect_harness_factors()['rules'][0]['basis'], 'keep')
        panel.harness_rule_rows[0]['factor'].set('0.85')
        self.assertNotIn('basis', panel.collect_harness_factors()['rules'][0])
        panel.add_harness_rule({'harness': 'codex', 'work_kind': 'debug', 'factor': 1})
        panel.remove_harness_rule(panel.harness_rule_rows[0])
        panel.exchange_vars['score_per_displaced_usd'].set('0.02')
        with mock.patch.object(router_data, 'save_tuning') as tuning_save, \
             mock.patch.object(router_data, 'save_strategy', return_value='saved') as save:
            panel.preset_buttons['省额度'].invoke()
            tuning_save.assert_not_called()
            save.assert_not_called()
            self.assertEqual(panel.harness_rule_rows[0]['harness'].get(), 'codex')
            self.assertEqual(panel.exchange_vars['score_per_displaced_worker_hour'].get(), '0.06')
            panel.run.reset_mock()
            panel.strategy_save.invoke()
            work, done = panel.run.call_args.args[:2]
            done(work())
        exchange = save.call_args.kwargs['opportunity_exchange']
        self.assertEqual(exchange['score_per_displaced_usd'], '0.02')
        self.assertEqual(exchange['score_per_displaced_worker_hour'], '0.06')
        self.assertNotIn('shortage_loss_per_usd', exchange)
        self.assertEqual(save.call_args.kwargs['harness_factors']['rules'], [
            {'harness': 'codex', 'work_kind': 'debug', 'factor': '1'}])
        self.assertEqual(panel.status.get(), 'saved')
        panel.change_language('en')
        self.assertEqual(panel.harness_title.cget('text'), 'Harness factor')
        self.assertEqual(panel.exchange_title.cget('text'), 'Opportunity exchange')
        self.assertEqual(panel.strategy_save.cget('text'), 'Save strategy & DNA')

    def test_harness_columns_share_one_grid_and_invalid_factor_blocks_save(self):
        panel = self.open()
        panel.show_setting('strategy')
        panel._factor_choices = {'harness': ['codex', 'pi'], 'work_kind': ['debug_review'],
                                 'model': ['gpt-6.1-sol']}
        panel.value_loaded((
            {'default': 1.0, 'rules': [{'harness': 'codex', 'model_prefix': 'claude', 'factor': 0.9}]},
            {'score_per_displaced_usd': 0.012, 'score_per_displaced_worker_hour': 0.06},
            panel._factor_choices))
        row = panel.harness_rule_rows[0]
        for key, header in panel.harness_headers.items():
            self.assertEqual(header.grid_info()['column'], row['widgets'][key].grid_info()['column'])
            self.assertEqual(header.grid_info()['sticky'], row['widgets'][key].grid_info()['sticky'])
            self.assertNotEqual(header.grid_info()['column'], row['delete'].grid_info()['column'])
        self.assertEqual(row['delete'].grid_info()['column'], 5)
        self.assertEqual(row['widgets']['harness'].winfo_class(), 'TCombobox')
        self.assertEqual(row['widgets']['work_kind'].winfo_class(), 'TCombobox')
        self.assertEqual(row['widgets']['model'].winfo_class(), 'TCombobox')
        self.assertEqual(row['widgets']['model_prefix'].winfo_class(), 'TEntry')
        self.assertEqual(row['widgets']['harness'].cget('values')[0], '（任意）')
        self.assertIn('pi', row['widgets']['harness'].cget('values'))
        self.assertEqual(row['work_kind'].get(), '（任意）')
        row['factor'].set('3')
        self.assertEqual(row['widgets']['factor'].cget('style'), 'Invalid.TEntry')
        with mock.patch.object(router_data, 'save_strategy') as save:
            panel.strategy_save.invoke()
            save.assert_not_called()
        self.assertIn('0', panel.status.get())
        self.assertIn('2', panel.status.get())
        row['factor'].set('2')
        self.assertEqual(row['widgets']['factor'].cget('style'), 'TEntry')
        panel.harness_default.set('9')
        self.assertEqual(panel.harness_default_entry.cget('style'), 'Invalid.TEntry')
        with mock.patch.object(router_data, 'save_strategy') as save:
            panel.strategy_save.invoke()
            save.assert_not_called()

    def test_latest_cli_read_wins_over_a_slower_read(self):
        panel = self.open()
        panel.run.reset_mock()
        panel.load_cli()
        old = panel.run.call_args.args[1]
        panel.load_cli()
        new = panel.run.call_args.args[1]
        new([])
        old(CLI_ROWS)
        self.assertEqual(panel.cli_inventory, [])

    def test_update_completion_discards_a_snapshot_started_before_the_update(self):
        panel = self.open()
        panel.load_cli()
        old = panel.run.call_args.args[1]
        with mock.patch.object(cli_updates, 'update_all', return_value=RESULT):
            panel.update_all_cli()
            work, done = panel.run.call_args.args[:2]
            done(work())
            fresh = panel.run.call_args.args[1]
            latest = [{**CLI_ROWS[0], 'version': '9.9.9', 'status': 'current'}]
            fresh(latest)
            old(CLI_ROWS)
        self.assertEqual(panel.cli_inventory, latest)
        self.assertEqual(panel.cli_status.get(), RESULT['summary'])

    def test_chip_layout_uses_available_width_and_reuses_widgets(self):
        panel = self.open()
        sample = row()
        sample['models'] = [{**sample['models'][0], 'model': f'model-{i}', 'model_label': f'Selected model {i}'} for i in range(8)]
        panel.models_rows_loaded([sample])
        widgets = panel.model_widgets[0]
        labels = list(widgets['chip_labels'])
        with mock.patch.object(widgets['chips'], 'winfo_width', return_value=1500):
            panel.layout_selected_chips(widgets)
            wide = widgets['visible_chips']
        with mock.patch.object(widgets['chips'], 'winfo_width', return_value=300):
            panel.layout_selected_chips(widgets)
            narrow = widgets['visible_chips']
        self.assertGreater(wide, narrow)
        self.assertEqual(labels, widgets['chip_labels'])
        positions = [label.place_info() for label in labels[:narrow]]
        positions += [widgets['overflow'].place_info(), widgets['dropdown'].place_info()]
        self.assertEqual(int(positions[-1]['y']), int(positions[-2]['y']))
        self.assertLess(int(positions[-1]['x']) - int(positions[-2]['x']), 100)
        widgets['overflow'].invoke()
        self.assertEqual(panel.model_menu_key, ('codex', 'codex'))

    def test_dropdown_is_anchored_below_the_inline_edit_entry(self):
        panel = self.open()
        panel.models_rows_loaded([row()])
        edit = panel.model_widgets[0]['dropdown']
        with mock.patch.object(edit, 'winfo_rootx', return_value=100), \
             mock.patch.object(edit, 'winfo_rooty', return_value=80), \
             mock.patch.object(edit, 'winfo_height', return_value=25), \
             mock.patch.object(self.root, 'winfo_width', return_value=2000), \
             mock.patch.object(self.root, 'winfo_rootx', return_value=0), \
             mock.patch.object(self.root, 'winfo_screenheight', return_value=3000):
            edit.invoke()
        panel.model_menu.update_idletasks()
        self.assertTrue(panel.model_menu.geometry().endswith('+100+105'), panel.model_menu.geometry())

    def test_dropdown_bulk_actions_and_tagged_entries(self):
        panel = self.open()
        sample = row()
        sample['models'].append({'model': 'new-model', 'model_label': 'New Model', 'enabled': False, 'partial': False,
                                 'routed': True, 'tags': ['默认分', '估价'], 'availability': 'detected',
                                 'bundle_ids': ['new_high']})
        sample['models'][0]['availability'] = 'missing'
        panel.models_rows_loaded([sample])
        panel.model_widgets[0]['dropdown'].invoke()
        self.assertIn('未检测到', panel.model_menu_items[0][2].cget('text'))
        self.assertIn('默认分 · 估价', panel.model_menu_items[1][2].cget('text'))
        self.assertFalse(panel.model_menu_items[1][2].instate(['disabled']))
        with mock.patch.object(router_data, 'set_harness_models') as save:
            for action in ('all', 'invert', 'clear'):
                panel.run.reset_mock()
                panel.model_menu_actions[action].invoke()
                panel.run.call_args.args[0]()
            self.assertEqual([call.args for call in save.call_args_list],
                             [('codex', 'codex', 'all'), ('codex', 'codex', 'invert'), ('codex', 'codex', 'clear')])
        sample['models'][0]['enabled'] = False
        panel.models_rows_loaded([sample])
        self.assertEqual(panel.model_widgets[0]['chip_labels'][0].cget('text'), '未选模型')

    def test_account_directory_editor_validates_and_saves_one_account_rule(self):
        panel = self.open()
        panel.models_rows_loaded([row(), row('pi')])
        with mock.patch.object(router_data, 'read_account_cwd', return_value='^D:/projects(/|$)'):
            panel.model_widgets[0]['cwd'].invoke()
        self.assertIn('✓ D:/projects/example', panel.cwd_preview.get())
        panel.cwd_pattern.set('[')
        self.assertTrue(panel.cwd_save_button.instate(['disabled']))
        panel.cwd_pattern.set('^E:/PROJECTS(/|$)')
        self.assertFalse(panel.cwd_save_button.instate(['disabled']))
        with mock.patch.object(router_data, 'save_account_cwd', return_value='saved') as save:
            panel.run.reset_mock()
            panel.cwd_save_button.invoke()
            work, done = panel.run.call_args.args[:2]
            done(work())
            save.assert_called_once_with('codex', '^E:/PROJECTS(/|$)')

    def test_manual_model_refresh_bypasses_the_cache(self):
        panel = self.open()
        panel._catalog_refreshing = False
        panel.model_refresh_button.state(['!disabled'])
        with mock.patch.object(router_data, 'refresh_model_catalog', return_value=[row()]) as refresh:
            panel.run.reset_mock()
            panel.model_refresh_button.invoke()
            work, done = panel.run.call_args.args[:2]
            done(work())
            refresh.assert_called_once_with(force=True)

    def test_a_slower_batch_refresh_cannot_overwrite_a_newer_page(self):
        panel = self.open()
        panel.run.reset_mock()
        with mock.patch.object(cli_updates, 'update_all', return_value=RESULT):
            panel.update_all_cli()
            update_work, update_done = panel.run.call_args.args[:2]
            self.assertEqual(update_work(), RESULT)
            panel.run.reset_mock()
            update_done(RESULT)
            batch_apply = panel.run.call_args.args[1]
        newer = [{**CLI_ROWS[0], 'version': '9.9.9', 'status': 'current'}]
        panel.load_cli()
        fresh = panel.run.call_args.args[1]
        fresh(newer)
        batch_apply([{**CLI_ROWS[0], 'version': '0.1.0', 'status': 'outdated'}])
        self.assertEqual(panel.cli_inventory, newer)
        self.assertNotEqual(panel.cli_status.get(), RESULT['summary'])

    def test_real_worker_keeps_ui_responsive_and_delivers_progress(self):
        panel = self.open(real_worker=True)
        for _ in range(60):
            self.root.update()
            if panel.busy_jobs == 0:
                break
            time.sleep(.01)
        self.assertEqual(panel.busy_jobs, 0)
        gate = threading.Event()
        def install(progress):
            progress('checking fixture')
            gate.wait(10)
            return RESULT
        with mock.patch.object(cli_updates, 'update_all', side_effect=install):
            started = time.monotonic()
            panel.cli_update_button.invoke()
            self.assertLess(time.monotonic() - started, 1)
            self.root.update()
            gate.set()
            deadline = time.monotonic() + 10
            while panel.busy_jobs and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        for thread in panel.long_jobs:
            thread.join(5)
        self.assertEqual(panel.busy_jobs, 0)
        self.assertEqual(panel.cli_status.get(), RESULT['summary'])


if __name__ == '__main__':
    unittest.main()
