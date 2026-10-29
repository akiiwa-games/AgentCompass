"""Pure scene/render tests and a fake-clock lifecycle; no root/window is created."""
import copy
import math
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import decision_theatre as dt


def result():
    def candidate(id, model, score, excluded=()):
        return dict(id=id,bundle=dict(model=model),score=score,excluded=list(excluded),
                    components=dict(fit=.7,opportunity_cost=.03,boost=.1))
    return dict(task=dict(objective='# A real task\nprivate body',work_kind='research'),choice='a',
                ranking=[candidate('excluded','blocked',99,['offline']),candidate('b','beta',.6),
                         candidate('a','alpha',.9),candidate('a2','alpha',.8)])


def test_scene_preserves_real_choice_scores_and_deduplicates_models():
    source=result(); original=copy.deepcopy(source)
    scene=dt.scene_from_result(source)
    assert source == original
    assert scene.title == 'A real task'
    assert [c.id for c in scene.candidates] == ['a','a2','b','excluded']
    assert [c.id for c in scene.pair] == ['a','b']
    assert [c.name for c in dt.shown_candidates(scene)] == ['alpha','beta','blocked']
    assert scene.winner.id == 'a' and scene.winner.score == .9
    assert scene.candidates[-1].excluded == ('offline',)


def test_snapshot_is_not_recomputed_and_missing_fields_are_not_invented():
    source=result()
    row=dict(decision_snapshot=source,objective='fallback title')
    snap=dt.snapshot_result(row)
    assert snap['ranking'] is source['ranking']
    assert snap['theatre_source'] == 'snapshot'
    assert dt.snapshot_result(dict(choice='a')) is None
    assert dt.snapshot_result(dict(decision_snapshot=dict(ranking='bad'))) is None
    scene=dt.scene_from_result(dict(choice='unknown',ranking=[dict(id='a',score=None)]))
    assert scene.winner is None and scene.pair == ()
    assert scene.candidates[0].values == ()
    assert dt.scene_from_result({}).winner is None


def test_snapshot_replay_never_calls_allocator(monkeypatch):
    import router_data
    mock=Mock(side_effect=AssertionError('snapshot must not replan'))
    monkeypatch.setattr(router_data,'plan_task',mock)
    row=dict(decision_snapshot=result())
    assert router_data.replay(row)['ranking'] == row['decision_snapshot']['ranking']
    mock.assert_not_called()


def test_clock_boundaries_and_easing_are_continuous():
    assert dt.beat_at(-1)==('task',0)
    assert dt.beat_at(dt.DURATION)==('settle',1)
    assert dt.beat_at(100)==('settle',1)
    for name,start,end in dt.BEATS:
        assert dt.beat_at(start)==(name,0)
        assert 0 <= dt.beat_at(end-.001)[1] <= 1
    assert [dt.ease(i/20) for i in range(21)] == sorted(dt.ease(i/20) for i in range(21))
    positions=[dt.camera_position(i/60,630) for i in range(round(dt.DURATION*60)+1)]
    assert positions==sorted(positions)
    assert positions[0]==0 and positions[-1]==pytest.approx(630*dt.CAMERA_SPAN)
    assert max(b-a for a,b in zip(positions,positions[1:]))<5


def test_camera_is_one_c2_curve_that_creeps_and_never_stops_mid_film():
    step=.001
    times=[i*step for i in range(round(dt.DURATION/step)+1)]
    x=[dt.camera_position(t,630) for t in times]
    v=[(b-a)/step for a,b in zip(x,x[1:])]
    acc=[(b-a)/step for a,b in zip(v,v[1:])]
    # Continuous velocity and acceleration everywhere, including every ramp joint.
    assert max(abs(b-a) for a,b in zip(v,v[1:])) < .5
    assert max(abs(b-a) for a,b in zip(acc,acc[1:])) < 5
    # Never a dead stop: only the first and last instants are at rest.
    assert min(v[round(.25/step):round((dt.DURATION-.05)/step)]) > 0
    assert v[0] == pytest.approx(0,abs=1e-3) and v[-1] == pytest.approx(0,abs=1e-2)
    cruise=max(v)
    hold=v[round(1.0/step)]
    assert .02*cruise < hold < .08*cruise
    assert x[-1]==pytest.approx(630*dt.CAMERA_SPAN)
    assert dt.PAN_END <= dt.WINDOWS['fold'][0]


def test_master_tempo_is_smooth_slow_when_read_and_preserves_total_time():
    step=.001
    walls=[i*step for i in range(round(dt.DURATION/step)+1)]
    r=[dt.tempo(t) for t in walls]
    assert dt.story_time(0)==0 and dt.story_time(dt.DURATION)==dt.DURATION
    assert dt.story_time(dt.DURATION-1e-6)==pytest.approx(dt.DURATION,abs=1e-5)
    story=[dt.story_time(t) for t in walls]
    assert story==sorted(story)
    assert min(r)>.7 and max(r)<1.25
    assert max(abs(b-a) for a,b in zip(r,r[1:])) < .002
    # Slower through each readable moment than between them.
    for centre,_,amount in dt.SLOW_PASSAGES:
        assert dt.tempo(centre) < 1
    for centre,_ in dt.FAST_CENTRES:
        assert dt.tempo(centre) > 1


def test_title_breathes_but_stays_readable_before_the_kind():
    assert 1.2 <= dt.TITLE_HOLD-dt.TITLE_IN <= 1.5
    assert dt.DURATION <= 10
    assert all(start >= dt.TITLE_HOLD for name,(start,end) in dt.WINDOWS.items()
               if name not in ('appear','underline'))
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    calls=[]
    renderer.blit=lambda image,value,*a,**k: calls.append((value,k.get('alpha'),a[:2],k.get('scale')))
    titles=[]
    for i in range(round(dt.TITLE_IN*60),round(dt.TITLE_HOLD*60)+1):
        calls.clear(); renderer.frame(i/60)
        titles.append([x for x in calls if x[0]=='A real task'][0])
    assert all(alpha==1 for _,alpha,_,_ in titles)
    ys=[xy[1] for _,_,xy,_ in titles]; scales=[sc for *_,sc in titles]
    # Imperceptible drift (≤ 2 px, ≤ 1.2 %) that never stops.
    assert 0 < ys[0]-ys[-1] < 2*1.5 and 1 < scales[-1] < 1.012
    assert all(b<a for a,b in zip(ys,ys[1:]))
    # In wall time the readable title lasts at least as long as in story time.
    walls=[i/1000 for i in range(4000)]
    readable=[w for w in walls if dt.TITLE_IN <= dt.story_time(w) <= dt.TITLE_HOLD]
    assert readable[-1]-readable[0] >= 1.4


def test_deadline_clock_never_lets_timer_granularity_alter_motion():
    f=dt.FRAME
    # Content is the latest reached deadline: a 15.6 ms or 31 ms timer tick shows the
    # same displacement grid, a callback 1 ms early still counts, never runs ahead.
    assert dt.frame_time(0)==0
    assert dt.frame_time(5*f+.0109)==pytest.approx(5*f)
    assert dt.frame_time(5*f-.001)==pytest.approx(5*f)
    assert dt.frame_time(5*f-.004)==pytest.approx(4*f)
    assert dt.frame_time(100)==dt.DURATION
    for now in (0,.0031,.0166,.0167,.05):
        for index in range(1,6):
            delay=dt.next_delay_ms(0,now,index)
            assert delay>=1 and now+delay/1000 >= index*f-1e-9
            assert delay-1 < max(1,(index*f-now)*1000)


def test_driver_draws_each_deadline_once_and_restores_timer_resolution(monkeypatch):
    clock=[0.]
    monkeypatch.setattr(dt.time,'perf_counter',lambda: clock[0])
    t=driver(); drawn=[]
    t.draw=lambda: drawn.append(t.elapsed)
    t.play(result())
    assert t.timer.calls==['begin'] and drawn==[0]
    for wall in (.0156,.0312,.0315,.0469,.09):
        clock[0]=wall
        t.canvas.callbacks.pop(t.after_id)()
    assert [round(x/dt.FRAME,6) for x in drawn]==[0,1,2,5]
    t.skip()
    assert t.timer.calls==['begin','end'] and not t.timer.active


def test_motion_is_continuous_from_routes_to_the_settled_name():
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    probes=[]
    for i in range(round(dt.DURATION*60)+1):
        renderer.frame(i/60); probes.append(dict(renderer.probe))
    for key in ('leader','winner_label','title'):
        rows=[(i,p[key]) for i,p in enumerate(probes) if key in p]
        assert rows
        steps=[math.dist(a[:2],b[:2]) for (i,a),(j,b) in zip(rows,rows[1:]) if j==i+1]
        # No teleport: every key object moves less than 3% of the stage per frame.
        assert max(steps) < 630*.03
    scales=[p['winner_label'][2] for p in probes if 'winner_label' in p]
    assert max(abs(b-a) for a,b in zip(scales,scales[1:])) < .03
    assert scales[-1]==pytest.approx(1)


def test_effort_arrives_with_the_folded_name():
    source=result()
    for item in source['ranking']:
        item['bundle']['effort']='high'
    renderer=dt.TheatreRenderer(dt.scene_from_result(source),630,600,1.5)
    calls=[]
    renderer.blit=lambda image,value,*a,**k: calls.append((value,k.get('alpha',1)))
    renderer.frame(dt.DURATION)
    assert [v for v,a in calls if a>0]==['alpha','· 高']
    calls.clear(); renderer.frame(dt.WINDOWS['fold'][0])
    assert '· 高' not in [v for v,a in calls if a>0]
    assert dt.WINDOWS['fold'][0] < dt.WINDOWS['effort'][0] < dt.WINDOWS['fold'][1]
    import i18n
    original=i18n.translate
    i18n.translate=lambda text: {'高':'High'}.get(text,text)
    try:
        assert dt.effort_label('high')=='· High'
    finally:
        i18n.translate=original
    assert dt.effort_label('')==''


def test_scroll_climax_is_only_the_chosen_name_and_keeps_true_title_phrase():
    assert dt.short_title('执行任务：落地「真正的任务」与其它细节')=='真正的任务'
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    calls=[]
    renderer.blit=lambda image,value,*a,**k: calls.append(value) if k.get('alpha',1)>0 else None
    renderer.frame(dt.DURATION)
    assert calls==['alpha']


def test_paths_have_exact_endpoints_and_even_particle_velocity():
    import math
    path=dt.route_points((0,40),(200,230))
    assert path[0]==(0,40) and path[-1]==(200,230)
    distances=[math.dist(a,b) for a,b in zip(path,path[1:])]
    assert max(distances)/min(distances)<1.02


@pytest.mark.parametrize('size',[(330,310,1),(630,600,1.5),(900,700,2)])
def test_every_scene_renders_without_a_display(size):
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),*size)
    for t in (0,.8,1.15,1.8,2.1,2.8,3.2,4.4,4.7,5.7,6.7,7.2,8.4,9.1,dt.DURATION):
        image=renderer.frame(t)
        assert image.size == size[:2]
    assert len(renderer.tiles)<60


class FakeTimer:
    def __init__(self): self.calls=[]; self.active=False
    def begin(self):
        if not self.active: self.calls.append('begin'); self.active=True
    def end(self):
        if self.active: self.calls.append('end'); self.active=False


class FakeCanvas:
    def __init__(self):
        self.callbacks={}; self.cancelled=[]; self.mapped=True
    def after(self,delay,callback):
        key=str(len(self.callbacks)); self.callbacks[key]=callback; return key
    def after_cancel(self,key):
        self.cancelled.append(key); self.callbacks.pop(key,None)
    def winfo_ismapped(self): return self.mapped


def driver():
    theatre=dt.DecisionTheatre.__new__(dt.DecisionTheatre)
    theatre.canvas=FakeCanvas()
    theatre.after_id=None
    theatre.pending=theatre.running=theatre.dead=False
    theatre.elapsed=dt.DURATION
    theatre.scene=None; theatre.renderer=None
    theatre.draw=Mock()
    theatre.timer=FakeTimer(); theatre.log=None
    return theatre


def test_single_driver_replay_skip_resize_and_unmap_cancel_old_clock():
    t=driver()
    t.play(result()); assert len(t.canvas.callbacks)==1
    t.play(result()); assert len(t.canvas.callbacks)==1
    t.configure(SimpleNamespace()); assert not t.canvas.callbacks and t.elapsed==dt.DURATION
    t.play(result()); t.canvas.mapped=False
    # Tk removes the pending callback before invoking it.
    callback=t.canvas.callbacks.pop(t.after_id); callback()
    assert not t.canvas.callbacks and not t.running
    t.canvas.mapped=True; t.play(result()); t.skip()
    assert not t.canvas.callbacks and t.elapsed==dt.DURATION
    t.play(result(),False)
    assert not t.canvas.callbacks and t.elapsed==dt.DURATION


def test_pending_hidden_replay_and_destroy_do_not_leak():
    t=driver(); t.canvas.mapped=False
    t.play(result())
    assert t.pending and not t.canvas.callbacks
    t.canvas.mapped=True; t.mapped(None)
    assert t.running and len(t.canvas.callbacks)==1
    t.top=Mock(); t.escape_binding='key'
    t.destroy(SimpleNamespace(widget=t.canvas))
    assert t.dead and not t.canvas.callbacks
    t.top.unbind.assert_called_once_with('<Escape>','key')


def test_kind_resolves_below_the_underline_and_holds_readable_with_the_title():
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    s=renderer.s
    calls=[]
    renderer.blit=lambda image,value,*a,**k: calls.append((value,a[:2],k.get('alpha',1)))
    readable=0
    for i in range(round(dt.WINDOWS['gather'][1]*60)):
        calls.clear(); renderer.frame(i/60)
        shown={v:(xy,a) for v,xy,a in calls}
        if 'Research' in shown or '开放式方案调研' in shown:
            (_,y),alpha=shown.get('开放式方案调研',shown.get('Research'))
            # The kind's glyph box (12 px font) never reaches the underline's stroke.
            assert y-renderer.origin[1] >= 12*s
            if alpha>.95 and shown['A real task'][1]>.65:
                readable+=1
    assert readable/60 >= .5
    # The underline's centre is the node: the routes start exactly there.
    assert renderer.origin==(renderer.title_at[0],renderer.title_at[1]+22*s)


def test_gather_never_leaves_an_empty_stage_and_the_node_grows_from_arriving_ink():
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    paper=renderer.blank.reduce(renderer.k)
    from PIL import ImageChops
    growth=[]
    for i in range(round(2.5*60),round(4.2*60)):
        t=i/60
        frame=renderer.frame(t)
        box=ImageChops.difference(frame.crop((0,0,630,560)),paper.crop((0,0,630,560))).getbbox()
        assert box is not None, t
        growth.append(renderer.node_growth(t))
    assert growth==sorted(growth) and growth[0]==0 and growth[-1]==pytest.approx(1)


def test_leading_speck_hands_over_to_its_route_head_at_matched_speed():
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    points=[]
    for i in range(round(2.9*60),round(4.6*60)):
        renderer.frame(i/60)
        if 'head' in renderer.probe:
            points.append(renderer.probe['head'])
    steps=[math.dist(a,b) for a,b in zip(points,points[1:])]
    assert len(steps)>40
    # Speed changes smoothly through the node: no jump when the speck becomes the route head.
    assert max(abs(b-a) for a,b in zip(steps,steps[1:])) < 1.5
