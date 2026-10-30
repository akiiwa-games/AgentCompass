import pytest
import decision_summary as summary
from test_decision_theatre import result
import router_data


def test_score_bars_share_a_linear_scale():
    origin,ends=summary.score_segments([.5,.45])
    assert origin==0 and ends==[1.,.9]
    assert summary.score_segments([0,0])==(0,[0,0])
    origin,ends=summary.score_segments([-.2,.2])
    assert origin==.5 and ends==[0,1]
    assert summary.score_segments([None,float('nan')])==(0,[0,0])
    origin,ends=summary.score_segments([.5,.45],.3)
    assert origin==0 and ends==pytest.approx([1,.75])


def test_quota_remaining_and_task_slice_do_not_overflow():
    assert summary.quota_segments(40,3.5)==pytest.approx((.365,.4))
    assert summary.quota_segments(3,10)==(0,.03)
    assert summary.quota_segments(120,-5)==(1,1)
    assert summary.quota_segments(None,3) is None


def test_static_strip_renders_without_tk_and_keeps_missing_quota_honest():
    data=result(); pair=router_data.decision_pair(data)
    model=summary.summary_model(data,pair)
    assert model['quota'] is None and model['fallback']
    renderer=summary.SummaryRenderer(model,630,1.5)
    assert renderer.render().size==(630,162)


def test_short_names_for_known_bundles_and_raw_id_fallbacks(tmp_path,monkeypatch):
    import i18n
    import display_names as names
    # The language preference is persisted; keep it in a temporary state directory.
    monkeypatch.setattr(router_data,'STATE_DIR',tmp_path)
    bundle=dict(bundle_id='opus55_high',model='claude-opus-5-5',effort='high')
    language=i18n.get_language()
    try:
        i18n.set_language('en')
        assert names.candidate_label(bundle)=='Opus 5.5 · High'
        assert names.kind_label('complex_build_plan')=='Build planning'
        assert names.choice_label(dict(choice='opus55_high',bundle=bundle))=='Opus 5.5 · High'
        i18n.set_language('zh')
        assert names.candidate_label(bundle)=='Opus 5.5 · 高'
        assert names.kind_label('complex_build_plan')=='复杂构建规划'
        # The winner is found in the snapshot ranking when the row has no bundle.
        row=dict(choice='opus55_high',decision_snapshot=dict(ranking=[dict(id='opus55_high',bundle=bundle)]))
        assert names.choice_label(row)=='Opus 5.5 · 高'
    finally:
        i18n.set_language(language)
    # Unknown bundles and kinds fall back to the raw id.
    assert names.choice_label(dict(choice='mystery_bundle'))=='mystery_bundle'
    assert names.candidate_label(dict(id='mystery_bundle'))=='mystery_bundle'
    assert names.kind_label('new_kind')=='new_kind'
    rows=[dict(id='a',model='claude-opus-5-5',effort='high',account='claude'),
          dict(id='b',model='claude-opus-5-5',effort='high',account='claude-work'),
          dict(id='c',model='claude-opus-5-5',effort='high',account='claude-work')]
    high=names.effort_text('high')
    assert names.unique_labels(rows[:2],router_data.account_label)==[f'Opus 5.5 · {high} · Claude',f'Opus 5.5 · {high} · Claude Work']
    assert names.unique_labels(rows,router_data.account_label)==[f'Opus 5.5 · {high} · Claude','b','c']
    summary_row=router_data.ledger_summary(dict(kind='complex_build_plan',choice='opus55_high',bundle=bundle))
    assert (summary_row['kind'],summary_row['choice'])==('complex_build_plan','opus55_high')
    assert (summary_row['kind_label'],summary_row['choice_label'])==(names.kind_label('complex_build_plan'),f'Opus 5.5 · {high}')
