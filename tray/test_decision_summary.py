import decision_summary as summary
from test_decision_theatre import result
import router_data


def test_static_strip_renders_without_tk_and_carries_only_names_and_the_task():
    data=result(); pair=router_data.decision_pair(data)
    model=summary.summary_model(data,pair)
    assert set(model)=={'names','task'} and len(model['names'])==2
    renderer=summary.SummaryRenderer(model,630,1.5)
    image=renderer.render()
    assert image.size==(630,123)
    # No bars: right of the names the strip is bare card.
    card=image.getpixel((625,2))
    assert all(image.getpixel((x,y))==card for x in range(380,630,5) for y in range(0,123,3))


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
    # A fast-mode variant (id = bundle id + '+fast', bundle record keeps the base id) shows its model plus a fast marker, not the raw id.
    fast_bundle={**bundle,'fast':True}
    fast_row=dict(choice='opus55_high+fast',bundle=fast_bundle)
    language=i18n.get_language()
    try:
        i18n.set_language('en')
        assert names.choice_label(fast_row)=='Opus 5.5 · High · fast'
        assert names.candidate_label(fast_bundle)=='Opus 5.5 · High · fast'
        assert names.choice_label(dict(choice='opus55_high+fast',decision_snapshot=dict(ranking=[dict(id='opus55_high+fast',bundle=fast_bundle)])))=='Opus 5.5 · High · fast'
        i18n.set_language('zh')
        assert names.choice_label(fast_row)=='Opus 5.5 · 高 · 快速'
    finally:
        i18n.set_language(language)
    # the base and its twin stay distinguishable in a candidate list, and a bundle id that is not the choice's base is still the raw id
    twin=[dict(id='opus55_high',model='claude-opus-5-5',effort='high'),dict(id='opus55_high+fast',model='claude-opus-5-5',effort='high',fast=True)]
    assert len(set(names.unique_labels(twin))) == 2 and 'opus55_high+fast' not in names.unique_labels(twin)
    assert names.choice_label(dict(choice='other_bundle+fast',bundle=fast_bundle))=='other_bundle+fast'
    rows=[dict(id='a',model='claude-opus-5-5',effort='high',account='claude'),
          dict(id='b',model='claude-opus-5-5',effort='high',account='claude-work'),
          dict(id='c',model='claude-opus-5-5',effort='high',account='claude-work')]
    high=names.effort_text('high')
    assert names.unique_labels(rows[:2],router_data.account_label)==[f'Opus 5.5 · {high} · Claude',f'Opus 5.5 · {high} · Claude Work']
    assert names.unique_labels(rows,router_data.account_label)==[f'Opus 5.5 · {high} · Claude','b','c']
    summary_row=router_data.ledger_summary(dict(kind='complex_build_plan',choice='opus55_high',bundle=bundle))
    assert (summary_row['kind'],summary_row['choice'])==('complex_build_plan','opus55_high')
    assert (summary_row['kind_label'],summary_row['choice_label'])==(names.kind_label('complex_build_plan'),f'Opus 5.5 · {high}')
