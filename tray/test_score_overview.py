"""The scoring page's data: the four categories, model levels on the 1..3 scale and the manual-score path (hermetic)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/agent-compass/scripts'))
import axis_scores
import router_data
import score_overview as so
import user_scores

TARGETS = {
    'divergence': {'levels': {}, 'continuity': 2.0,
                   'facets': {'creative_writing': {'levels': {'1': {'target': -3.0, 'statistic': 'panel minimum'},
                                                              '2': {'target': -2.0, 'statistic': 'panel median'},
                                                              '3': {'target': 0.0, 'statistic': 'anchor'}}}}},
    'coding': {'continuity': 1.5, 'facets': {
        'execution': {'levels': {'1': {'target': -3.0}, '2': {'target': -1.5}, '3': {'target': 0.0}}},
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


def test_model_rows_read_levels_through_z_to_level():
    rows = so.model_rows({'m': model(-2.0), 'top': model(0.5)}, TARGETS)
    by = {r['model']: r for r in rows}
    assert by['m']['cells']['divergence']['level'] == pytest.approx(2.0)
    assert so.cell_text(by['top']['cells']['divergence']) == '3.0+'
    # tool_operation: the weakest of its level-3 facets; responsiveness above t3 is at least the median
    assert by['m']['cells']['tool_operation']['level'] == pytest.approx(
        min(user_scores.z_to_level(-2.0, -2.8, -0.7, 0.0), user_scores.z_to_level(0.5, -1.6, 0.1, 0.1)))
    tip = so.cell_tip('M', 'divergence', by['m']['cells']['divergence'])
    assert 'lmarena.creative_writing — Model X (High)' in tip
    assert '3.0+' in tip


def test_estimated_and_user_cells_are_marked():
    row = so.model_rows({'m': model(-2.0, 'estimated')}, TARGETS)[0]
    assert so.cell_text(row['cells']['divergence']).endswith('~')
    row = so.model_rows({'m': model(-2.0, 'user')}, TARGETS)[0]
    assert so.cell_text(row['cells']['divergence']).endswith('✎')
    assert so.current_manual(row, 'divergence')[1] == 'user'
    assert so.source_label('user') == '本机覆盖'


def test_category_overview_lists_boards_dates_targets_and_continuity():
    data = {'anchor': {'targets': TARGETS},
            'links': {'coding.execution': {'boards': ['aa.a', 'arena.b'], 'panel_n': 12}},
            'source_ages': {'aa.a': {'as_of': '2026-11-09'}, 'arena.b': {'as_of': '2026-10-16'}}}
    blocks = {b['key']: b for b in so.category_overview(data, {'m': model(-2.0), 'n': model(-1.0, 'estimated')})}
    execution = blocks['coding']['facets'][0]
    assert execution['boards'] == ['aa.a', 'arena.b']
    assert so.facet_dates_text(execution) == '2026-10-16 – 2026-11-09'
    assert execution['targets']['2'][0] == -1.5
    assert blocks['coding']['continuity'] == 1.5
    assert '1.500' in blocks['coding']['derivation']
    assert blocks['divergence']['coverage'] == {'measured': 1, 'estimated': 1}


def test_targets_choices_follow_the_cli_targets():
    targets = [t for key, _ in so.CATEGORIES for t, _ in so.targets_choices(key)]
    assert set(targets) == set(user_scores.VALID_TARGETS)


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


def test_score_overview_reads_the_shipped_data(tmp_path, monkeypatch):
    monkeypatch.setenv(user_scores.ENV_VAR, str(tmp_path / 'none.json'))
    data = router_data.score_overview()
    assert [c['key'] for c in data['categories']] == list(axis_scores.FIVE_AXES)
    assert data['models'] and all(set(r['cells']) == set(axis_scores.FIVE_AXES) for r in data['models'])
