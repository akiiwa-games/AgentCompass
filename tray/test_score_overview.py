"""The scoring page's data: the four categories, model levels on the 1..3 scale and the manual-score path (hermetic)."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import account_fleet
import allocator
import axis_scores
import display_names
import i18n
import quota_economy
import radar_chart
import router_data
import score_overview as so
import user_scores

TARGETS = {
    'divergence': {'levels': {}, 'continuity': 2.0,
                   'facets': {'creative_writing': {'levels': {'1': {'target': -3.0, 'statistic': 'panel minimum'},
                                                              '2': {'target': -2.0, 'statistic': 'panel median'},
                                                              '3': {'target': 0.0, 'statistic': 'anchor'}}}}},
    'coding': {'continuity': 1.5, 'facets': {
        'execution': {'levels': {'1': {'target': -3.0}, '2': {'target': -1.5},
                                 '3': {'target': 0.0, 'ceiling': {'value': 0.42, 'model': 'best-model', 'status': 'measured'}}}},
        'faithfulness': {'levels': {'1': {'target': -2.0}, '2': {'target': -0.5}, '3': {'target': 0.0}}}}},
    'aesthetics': {'continuity': 1.0, 'facets': {
        'design': {'levels': {'1': {'target': -2.0}, '2': {'target': -1.0}, '3': {'target': 0.0}}}}},
    'tool_operation': {'continuity': 0.7, 'facets': {
        'responsiveness': {'levels': {'1': {'target': -1.6}, '2': {'target': 0.1}, '3': {'target': 0.1}}},
        'image_recognition': {'levels': {'1': {'target': -2.8}, '2': {'target': -0.7}, '3': {'target': 0.0}}}}},
}
for _cat in TARGETS.values():
    first = next(iter(_cat['facets'].values()))['levels']
    _cat['levels'] = first


def cell(value, status='measured', **extra):
    return {'value': value, 'by_effort': {'high': value}, 'status': status, **extra}


def model(z, status='measured'):
    return {'five_axis': {
        'categories': {
            'divergence': {**cell(z, status), 'facets': {'creative_writing': cell(z, status, sources={
                'lmarena.creative_writing': {'listed_as': 'Model X (High)'}})}},
            'coding': {**cell(z), 'facets': {'execution': cell(z), 'faithfulness': cell(z)}},
            'aesthetics': {**cell(z), 'facets': {'design': cell(z)}},
            'tool_operation': {'facets': {'responsiveness': cell(0.5), 'image_recognition': cell(z)}}},
        'axis_status': {'divergence': status, 'coding': 'measured', 'aesthetics': 'measured', 'tool_operation': 'measured'}}}


def test_level_text_clamps_above_three():
    assert so.level_text(None) == '—'
    assert so.level_text(2.04) == '2.0'
    assert so.level_text(3.0) == '3.0'
    assert so.level_text(3.4) == '3.0+'


def test_category_names_are_the_current_four_and_creativity_is_chuangyi():
    assert [key for key, _ in so.CATEGORIES] == list(axis_scores.FIVE_AXES)
    assert so.CATEGORIES[0][1] == '创意'


def test_radar_axes_use_display_names_in_both_languages():
    prev = i18n.get_language()
    try:
        i18n.set_language('zh')
        for seq in (radar_chart.SCORE_AXES, radar_chart.DNA_AXES):
            formulas = [formula for _, _, formula in seq]
            assert formulas[0].startswith('创意：')
            assert not any(f.startswith(('divergence', 'coding', 'aesthetics', 'tool_operation')) for f in formulas)
        i18n.set_language('en')
        for seq in (radar_chart.SCORE_AXES, radar_chart.DNA_AXES):
            formulas = [formula for _, _, formula in seq]
            assert formulas[0].startswith('Creativity:')
            assert not any(f.startswith(('divergence:', 'coding:', 'aesthetics:', 'tool_operation:')) for f in formulas)
    finally:
        i18n.set_language(prev)


def test_model_rows_read_levels_through_z_to_level():
    rows = so.model_rows({'m': model(-2.0), 'top': model(0.5)}, TARGETS)
    by = {r['model']: r for r in rows}
    assert by['m']['cells']['divergence']['level'] == pytest.approx(2.0)
    assert so.cell_text(by['top']['cells']['divergence']) == '3.0+'
    # tool_operation: the weakest of its existing facets; responsiveness above t3 is at least the median
    assert by['m']['cells']['tool_operation']['level'] == pytest.approx(
        min(user_scores.z_to_level(-2.0, -2.8, -0.7, 0.0), user_scores.z_to_level(0.5, -1.6, 0.1, 0.1)))
    tip = so.cell_tip('M', 'divergence', by['m']['cells']['divergence'], model_key='m-raw')
    assert 'M (m-raw) · 创意' in tip
    assert 'lmarena.creative_writing — Model X (High)' in tip
    assert '3.0+' in tip


def test_cells_show_only_levels_while_hover_and_editor_show_deciding_source():
    # Parent steer + Finding A: table cells show plain levels only (no ✎ ◆ ~ markers); source comes from deciding facet.
    for status in ('estimated', 'user', 'maintainer', 'measured'):
        row = so.model_rows({'m': model(-2.0, status)}, TARGETS)[0]
        assert so.cell_text(row['cells']['divergence']) == '2.0'
    assert so.SOURCE_OF['partial'] == '部分'
    assert so.source_label('partial') != '实测'

    # Finding A: coding = min(execution, faithfulness); when measured execution < maintainer faithfulness, source is 实测.
    mixed = model(-1.0)
    mixed['five_axis']['categories']['coding'] = {
        **cell(-1.8, 'maintainer'),
        'facets': {'execution': cell(-1.8, 'measured'), 'faithfulness': cell(0.0, 'maintainer')},
    }
    mixed['five_axis']['axis_status']['coding'] = 'maintainer'
    row = so.model_rows({'gpt-6-luna': mixed}, TARGETS, label=lambda _: 'Luna 6')[0]
    coding_cell = row['cells']['coding']
    assert coding_cell['deciding_facet'] == 'execution'
    assert coding_cell['status'] == 'measured'
    assert so.source_label(coding_cell['status']) == '实测'
    tip = so.cell_tip(row['label'], 'coding', coding_cell, model_key=row['model'])
    assert 'Luna 6 (gpt-6-luna) · 编码能力' in tip
    assert '来源：实测' in tip


def test_category_scale_and_deciding_facet_level_appear_in_hover_and_editor():
    # Finding B: faithfulness z=-2.0 is level 1.0 on faithfulness, but ~1.7 on coding's category targets.
    m = model(-0.2)
    m['five_axis']['categories']['coding'] = {
        **cell(-2.0, 'user'),
        'facets': {'execution': cell(-0.2, 'measured'), 'faithfulness': cell(-2.0, 'user')},
    }
    row = so.model_rows({'sol': m}, TARGETS, label=lambda _: 'Sol')[0]
    coding_cell = row['cells']['coding']
    assert coding_cell['level'] == pytest.approx(user_scores.level_for_target_z('coding', -2.0, TARGETS))
    assert coding_cell['deciding_facet'] == 'faithfulness'
    assert coding_cell['deciding_level'] == pytest.approx(1.0)
    tip = so.cell_tip('Sol', 'coding', coding_cell, model_key='sol')
    assert '决定项 忠实度 1.0' in tip
    assert '类别等级按路由器的类别目标换算' in tip
    editor_line = so.editor_current_text(row, 'coding')
    assert '决定项 忠实度 1.0' in editor_line
    assert '来源：本机覆盖' in editor_line
    assert '类别等级按路由器的类别目标换算' in editor_line


def test_partial_category_shows_existing_facet_level_and_marks_missing_facet_in_hover(tmp_path, monkeypatch):
    # Finding C: one missing facet shows the existing facet's level and marks the missing facet in hover; all-missing -> "—".
    speed_only = model(-1.0)
    speed_only['five_axis']['categories']['tool_operation'] = {
        'facets': {'responsiveness': cell(0.1, 'measured'), 'image_recognition': {'status': 'missing'}}
    }
    vision_only = model(-1.0)
    vision_only['five_axis']['categories']['tool_operation'] = {
        'facets': {'responsiveness': {'status': 'missing'}, 'image_recognition': cell(-0.7, 'measured')}
    }
    none_present = model(-1.0)
    none_present['five_axis']['categories']['tool_operation'] = {
        'facets': {'responsiveness': {'status': 'missing'}, 'image_recognition': {'status': 'missing'}}
    }
    rows = {r['model']: r for r in so.model_rows(
        {'glm-5.3': speed_only, 'beam-free': vision_only, 'glm-5.2': none_present}, TARGETS)}
    assert so.cell_text(rows['glm-5.3']['cells']['tool_operation']) == '2.0'
    assert '缺失子维度：图像识别' in so.cell_tip('GLM 5.3', 'tool_operation', rows['glm-5.3']['cells']['tool_operation'])
    assert so.cell_text(rows['beam-free']['cells']['tool_operation']) == '2.0'
    assert '缺失子维度：响应速度' in so.cell_tip('Beam Free', 'tool_operation', rows['beam-free']['cells']['tool_operation'])
    assert so.cell_text(rows['glm-5.2']['cells']['tool_operation']) == '—'

    monkeypatch.setenv(user_scores.ENV_VAR, str(tmp_path / 'none.json'))
    shipped = {r['model']: r for r in router_data.score_overview()['models']}
    assert so.cell_text(shipped['glm-5.3']['cells']['tool_operation']) != '—'
    assert so.cell_text(shipped['beam-free']['cells']['tool_operation']) != '—'
    assert so.cell_text(shipped['glm-5.2']['cells']['tool_operation']) == '—'


def test_category_overview_lists_boards_dates_targets_and_continuity():
    data = {'anchor': {'targets': TARGETS},
            'links': {'coding.execution': {'boards': ['aa.a', 'arena.b'], 'panel_n': 12}},
            'source_ages': {'aa.a': {'as_of': '2026-11-09'}, 'arena.b': {'as_of': '2026-10-16'}}}
    blocks = {b['key']: b for b in so.category_overview(data, {'m': model(-2.0), 'n': model(-1.0, 'estimated')})}
    execution = blocks['coding']['facets'][0]
    assert execution['boards'] == ['aa.a', 'arena.b']
    assert so.facet_dates_text(execution) == '2026-10-16 – 2026-11-09'
    assert execution['targets']['2'][0] == -1.5
    assert '面板标准分 z' in so.targets_tip(execution)
    assert '+0.4200' in so.targets_tip(execution) and 'best-model' in so.targets_tip(execution)
    assert blocks['coding']['continuity'] == 1.5
    assert '1.500' in blocks['coding']['derivation']
    assert blocks['divergence']['coverage'] == {'measured': 1, 'estimated': 1}


def test_demand_grid_n_and_live_economy_text_are_derived_from_router_data():
    n = so.demand_grid_n()
    assert n == len(quota_economy.FIVE_AXIS_DEMAND_GRID)
    tip = so.economy_tip([{'label': 'claude/weekly', 'used_usd': 120.0, 'capacity_usd': 400.0,
                           'recovery_line_usd': 180.0, 'need_probability': 0.42, 'quota_price': 0.0123}], n)
    assert f'{n} 点需求网格' in tip
    assert 'claude/weekly' in tip
    text = so.economy_text([{'label': 'claude/weekly', 'used_usd': 120.0, 'capacity_usd': 400.0,
                             'recovery_line_usd': 180.0, 'quota_price': 0.0123}], n)
    assert 'claude/weekly' in text and '$120/$400' in text


def test_catalog_drives_every_model_display_name_and_hover_shows_real_key():
    doc = json.loads((account_fleet.REFERENCES / 'accounts.json').read_text(encoding='utf-8'))
    labels = display_names.catalog_labels()
    models = doc['models']
    assert set(models).issubset(labels)
    assert len({labels[k] for k in models}) == len(models)
    for key in models:
        shown = display_names.model_label(key)
        assert shown == labels[key]
        row = {'model': key, 'label': shown}
        if shown != key:
            assert key in so.model_tip(row)


def test_targets_choices_follow_the_cli_targets():
    targets = [t for key, _ in so.CATEGORIES for t, _ in so.targets_choices(key)]
    assert set(targets) == set(user_scores.VALID_TARGETS)


def test_editor_prefill_is_never_lossy_and_category_clear_warns_when_facet_overrides_remain(tmp_path, monkeypatch):
    local = tmp_path / 'user-scores.json'
    monkeypatch.setenv(user_scores.ENV_VAR, str(local))

    # Multi-facet category without a whole-category manual score must not prefill an aggregated level.
    initial = {r['model']: r for r in router_data.score_overview()['models']}['gpt-6.1-sol']
    assert so.prefill_level(initial, 'coding') is None
    assert so.prefill_level(initial, 'tool_operation') is None
    # Clamped 3.0+ and collapsed responsiveness > t3 also do not prefill lossy numbers.
    top_row = so.model_rows({'top': model(0.5)}, TARGETS)[0]
    assert so.prefill_level(top_row, 'divergence') is None
    assert so.prefill_level(top_row, 'tool_operation.responsiveness') is None

    # Whole-category tool_operation = 3.0 prefills exact 3.0 (not the 2.0 responsiveness ceiling).
    router_data.set_manual_score('gpt-6.1-sol', 'tool_operation', 3.0)
    after_tool = {r['model']: r for r in router_data.score_overview()['models']}['gpt-6.1-sol']
    assert so.prefill_level(after_tool, 'tool_operation') == 3.0
    router_data.clear_manual_score('gpt-6.1-sol', 'tool_operation')

    # Clearing whole category when only a facet override exists warns that the facet override remains.
    router_data.set_manual_score('gpt-6.1-sol', 'coding.faithfulness', 1.5)
    after_facet = {r['model']: r for r in router_data.score_overview()['models']}['gpt-6.1-sol']
    assert so.prefill_level(after_facet, 'coding') is None
    assert so.prefill_level(after_facet, 'coding.faithfulness') == 1.5
    msg = router_data.clear_manual_score('gpt-6.1-sol', 'coding')
    assert '0' in msg and 'coding.faithfulness' in msg
    assert 'coding.faithfulness' in json.loads(local.read_text(encoding='utf-8'))['scores']['gpt-6.1-sol']
    cleared = router_data.clear_manual_score('gpt-6.1-sol', 'coding.faithfulness')
    assert '1' in cleared


def test_manual_score_goes_through_the_cli_writer_to_the_local_file_only(tmp_path, monkeypatch):
    local = tmp_path / 'user-scores.json'
    public = tmp_path / 'public.json'
    monkeypatch.setenv(user_scores.ENV_VAR, str(local))
    monkeypatch.setattr(user_scores, 'PUBLIC_SCORES_FILE', public)
    router_data.set_manual_score('gpt-x', 'coding.faithfulness', 2.5)
    doc = json.loads(local.read_text(encoding='utf-8'))
    assert doc['scores']['gpt-x']['coding.faithfulness']['level'] == 2.5
    assert doc['scores']['gpt-x']['coding.faithfulness']['kind'] == 'user'
    assert not public.exists()
    router_data.clear_manual_score('gpt-x', 'coding.faithfulness')
    assert json.loads(local.read_text(encoding='utf-8'))['scores'] == {}


def test_manual_score_changes_allocator_plan_winner_when_reading_local_state(tmp_path, monkeypatch):
    import router_test_support
    local = tmp_path / 'user-scores.json'
    monkeypatch.setenv(user_scores.ENV_VAR, str(local))
    accounts = account_fleet.load_accounts(account_fleet.reference_fleet_path())
    accounts['accounts'] = {k: accounts['accounts'][k] for k in ('claude', 'codex')}
    now = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)
    usage = router_test_support.usage_windows(now, {
        'claude': [('five_hour', 95, 3), ('seven_day', 90, 100), ('weekly_fable', 100, 100)],
        'codex': [('usage', 90, 100)],
    })
    task = {
        'objective': 'implement local module fix',
        'expected_hours': 1.0,
        'needs': {'local_only': True, 'local_only_reason': 'test'},
        'profile': router_test_support.profile(local_only=True, axis_demand={
            'divergence': 1, 'coding': 3, 'aesthetics': 0, 'tool_operation': 2,
        }),
    }
    caps = account_fleet.load_capabilities(accounts=accounts)
    policy = {'allowed_accounts': ['claude', 'codex']}
    before = allocator.plan(task, usage, now, accounts=accounts, capabilities=caps,
                            host_policy_config=policy, read_local_state=True)
    winner_before = before['choice']
    winner_cand = next(c for c in before['ranking'] if c['id'] == winner_before)
    winner_model = winner_cand['bundle']['model']
    winner_account = winner_cand['bundle']['account']

    # Lowering the winner model's manual coding + tool_operation scores flips allocator.plan(read_local_state=True)
    # to the other account's model, while public-only planning (read_local_state=False) is unchanged.
    router_data.set_manual_score(winner_model, 'coding.faithfulness', 0.2)
    router_data.set_manual_score(winner_model, 'tool_operation', 0.2)
    after = allocator.plan(task, usage, now, accounts=accounts, capabilities=caps,
                           host_policy_config=policy, read_local_state=True)
    public_only = allocator.plan(task, usage, now, accounts=accounts, capabilities=caps,
                                 host_policy_config=policy, read_local_state=False)
    after_cand = next(c for c in after['ranking'] if c['id'] == after['choice'])
    assert after_cand['bundle']['account'] != winner_account
    assert public_only['choice'] == winner_before

    router_data.clear_manual_score(winner_model, 'coding.faithfulness')
    router_data.clear_manual_score(winner_model, 'tool_operation')
    restored = allocator.plan(task, usage, now, accounts=accounts, capabilities=caps,
                              host_policy_config=policy, read_local_state=True)
    assert restored['choice'] == winner_before


def test_score_overview_reads_the_shipped_data(tmp_path, monkeypatch):
    monkeypatch.setenv(user_scores.ENV_VAR, str(tmp_path / 'none.json'))
    data = router_data.score_overview()
    assert [c['key'] for c in data['categories']] == list(axis_scores.FIVE_AXES)
    assert data['demand_grid_n'] == len(quota_economy.FIVE_AXIS_DEMAND_GRID)
    assert isinstance(data['economy_windows'], list)
    assert data['models'] and all(set(r['cells']) == set(axis_scores.FIVE_AXES) for r in data['models'])


def test_live_economy_windows_reports_active_priced_windows(monkeypatch):
    _, router = router_data.modules()
    import host_policy
    import provider_inventory
    import router_test_support

    now = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)
    fake_usage = router_test_support.usage_windows(now, {
        'claude': [('five_hour', 90, 3), ('seven_day', 72, 100), ('weekly_fable', 100, 100)],
        'codex': [('usage', 81, 100)],
    })
    monkeypatch.setenv('ROUTER_ACCOUNTS_FILE', str(account_fleet.reference_fleet_path()))
    monkeypatch.setattr(provider_inventory, 'current', lambda *_a, **_kw: {'accounts': {}, 'launchers': {}})
    monkeypatch.setattr(host_policy, 'load_policy', lambda *_a, **_kw: {'allowed_accounts': ['claude', 'codex']})
    monkeypatch.setattr(router_data, 'now_utc', lambda: now)
    monkeypatch.setattr(router, 'usage_snapshot', lambda *_a, **_kw: fake_usage)
    monkeypatch.setattr(router, 'read_ledger', lambda *_a, **_kw: [])

    windows = router_data.live_economy_windows()
    labels = {w['label'] for w in windows}
    assert {'claude/seven_day', 'codex/usage'} <= labels
    text = so.economy_text(windows)
    assert 'claude/seven_day' in text and 'codex/usage' in text
