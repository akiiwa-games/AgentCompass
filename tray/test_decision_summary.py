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
