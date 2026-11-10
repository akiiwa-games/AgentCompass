"""Scene/render contracts, lifecycle, and opt-in diagnostics on a hidden real Tk canvas."""
import copy
import math
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

# Import our own module instance: a developer's optional import-time diagnostic
# must not wrap scene methods or write traces during deterministic render tests.
# os.path.expanduser uses USERPROFILE on Windows and HOME on Unix.
with tempfile.TemporaryDirectory(prefix="theatre test home ") as _home:
    with patch.dict(os.environ, {"HOME": _home, "USERPROFILE": _home}):
        _spec = importlib.util.spec_from_file_location(
            "_theatre_test_subject", Path(__file__).with_name("decision_theatre.py"))
        dt = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = dt
        _spec.loader.exec_module(dt)


def result():
    def candidate(id, model, score, excluded=()):
        return dict(id=id,bundle=dict(model=model),score=score,excluded=list(excluded),
                    components=dict(fit=.7,opportunity_cost=.03,boost=.1))
    return dict(task=dict(objective='# A real task\nprivate body',work_kind='research'),choice='a',
                ranking=[candidate('excluded','blocked',99,['offline']),candidate('b','beta',.6),
                         candidate('a','alpha',.9),candidate('a2','alpha',.8)])


def test_film_drives_its_companion_view_and_settles_it_when_stopped():
    seen=[]
    theatre=dt.DecisionTheatre.__new__(dt.DecisionTheatre)
    assert theatre.on_frame is None
    theatre.on_frame=seen.append
    theatre.after_id=None
    theatre.timer=SimpleNamespace(end=lambda:None)
    theatre.stop()
    assert seen==[dt.DURATION]


@pytest.mark.parametrize('diagnostics', ['off', 'on', 'unwritable'])
def test_optional_trace_preserves_render_skip_errors_and_close(tmp_path, monkeypatch, diagnostics):
    from tk_test_support import shared_tk_root, reset_tk_root
    state = tmp_path / '.agent-compass'
    state.mkdir()
    log = state / 'theatre-trace.log'
    if diagnostics != 'off':
        (state / 'theatre-trace.on').touch()
    if diagnostics == 'unwritable':
        log.mkdir()  # A real inaccessible log destination, not a mocked successful logger.
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    spec = importlib.util.spec_from_file_location('_theatre_trace_subject', Path(dt.__file__))
    subject = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, subject)
    spec.loader.exec_module(subject)
    root = shared_tk_root()
    theatre = subject.DecisionTheatre(root)
    try:
        theatre.play(result(), animate=False)
        theatre.draw()
        assert theatre.photo is not None and theatre.scene.winner.id == 'a'
        assert theatre.skip() == 'break'
        assert not theatre.running and not theatre.pending and theatre.after_id is None
        failure = RuntimeError('renderer failure remains visible')
        with patch.object(theatre.renderer, 'frame', side_effect=failure):
            with pytest.raises(RuntimeError) as caught:
                theatre.draw()
        assert caught.value is failure
        theatre.canvas.destroy()
        assert theatre.dead and theatre.photo is None and theatre.after_id is None
        if diagnostics == 'on':
            text = log.read_text(encoding='utf-8')
            assert 'play#' in text and 'draw#' in text and 'destroy#' in text
            assert 'EXC' in text and 'renderer failure remains visible' in text
        elif diagnostics == 'off':
            assert not log.exists()
    finally:
        reset_tk_root(root)


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
    assert dt.PAN_END <= dt.WINDOWS['settle'][0]


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


def test_effort_arrives_with_the_settled_name():
    source=result()
    for item in source['ranking']:
        item['bundle']['effort']='high'
    renderer=dt.TheatreRenderer(dt.scene_from_result(source),630,600,1.5)
    calls=[]
    renderer.blit=lambda image,value,*a,**k: calls.append((value,k.get('alpha',1)))
    renderer.frame(dt.DURATION)
    shown=[v for v,a in calls if a>0]
    assert shown[:2]==['alpha','beta'] and shown[-1]=='· 高'
    calls.clear(); renderer.frame(dt.WINDOWS['settle'][0])
    assert '· 高' not in [v for v,a in calls if a>0]
    assert dt.WINDOWS['settle'][0] < dt.WINDOWS['effort'][0] < dt.WINDOWS['settle'][1]
    import i18n
    original=i18n.translate
    i18n.translate=lambda text: {'高':'High'}.get(text,text)
    try:
        assert dt.effort_label('high')=='· High'
    finally:
        i18n.translate=original
    assert dt.effort_label('')==''
    # A fast-mode bundle says so beside its effort.
    source['ranking'][2]['bundle']['fast']=True
    scene=dt.scene_from_result(source)
    assert scene.winner.fast and not scene.pair[1].fast
    assert dt.effort_label('', True)=='· '+dt._tr('快速')
    assert dt.TheatreRenderer(scene,630,600,1.5).final['effort'].endswith(dt._tr('快速'))


def test_final_frame_keeps_the_radar_with_its_axis_names_under_the_winner():
    assert dt.short_title('执行任务：落地「真正的任务」与其它细节')=='真正的任务'
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),630,600,1.5)
    calls=[]
    real=renderer.blit
    def blit(image,value,*a,**k):
        if k.get('alpha',1)>0:
            calls.append((value,a[:2],k.get('scale',1)))
        return real(image,value,*a,**k)
    renderer.blit=blit
    frame=renderer.frame(dt.DURATION)
    axes=[label for _,label,_ in dt.radar.SCORE_AXES]
    # Who won, against whom, and on which five axes: nothing else is written on the last frame.
    assert [v for v,*_ in calls]==['alpha','beta',*axes]
    shown={v:(xy,scale) for v,xy,scale in calls}
    assert shown['alpha'][1]==pytest.approx(1) and shown['beta'][1]==pytest.approx(dt.LEGEND/dt.NAME)
    lowest=renderer.center[1]+renderer.radius*math.cos(math.pi/5)
    assert lowest < shown['alpha'][0][1] < shown['beta'][0][1] < renderer.h-18*renderer.s
    # The radar is still drawn: the winner's filled shape covers the centre, and a spoke reaches the rim.
    cam=dt.camera_position(dt.DURATION,renderer.w)
    paper=renderer.blank.reduce(renderer.k).getpixel((0,0))
    assert frame.getpixel((round(renderer.center[0]-cam),round(renderer.center[1])))!=paper
    assert frame.getpixel((round(renderer.center[0]-cam),round(renderer.center[1]-renderer.radius*.9)))!=paper
    # There is no fixed backdrop: between two spokes, outside both shapes, the stage is bare paper.
    x,y=dt.radar.point(renderer.center[0]-cam,renderer.center[1],renderer.radius*.97,0,5)
    x2,y2=dt.radar.point(renderer.center[0]-cam,renderer.center[1],renderer.radius*.97,1,5)
    assert frame.getpixel((round((x+x2)/2),round((y+y2)/2)))==paper


@pytest.mark.parametrize('size',[(300,300,1),(330,310,1),(474,488,1.15),(630,600,1.5),(900,700,2)])
@pytest.mark.parametrize('names',[None,('Divergence','Global Rigor','Module Precision','Vision & Tools','Quota Economy')])
def test_axis_names_stay_inside_the_stage_and_clear_of_each_other(size,names,monkeypatch):
    if names:
        monkeypatch.setattr(dt.radar,'SCORE_AXES',tuple((str(i),name,'') for i,name in enumerate(names)))
    renderer=dt.TheatreRenderer(dt.scene_from_result(result()),*size)
    cam=dt.camera_position(dt.DURATION,renderer.w)
    boxes=[]
    for label,x,y,room in renderer.axis_labels:
        half=min(room,renderer.measure(label,dt.AXIS_LABEL,False,room))/2
        boxes.append((x-cam-half,y-7*renderer.s,x-cam+half,y+7*renderer.s))
    assert len(boxes)==5
    for left,top,right,bottom in boxes:
        assert 0<=left and right<=renderer.w and 0<=top and bottom<renderer.final['card'][1]
    for i,a in enumerate(boxes):
        for b in boxes[i+1:]:
            assert a[2]<=b[0] or b[2]<=a[0] or a[3]<=b[1] or b[3]<=a[1]
    # Every name is shown in full: a narrow stage draws a smaller radar instead of shortening a name.
    assert all(renderer.measure(label,dt.AXIS_LABEL,False)<=room+.5 for label,_,_,room in renderer.axis_labels)


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


def test_a_replay_on_a_covered_page_waits_for_the_page_and_stops_when_covered_again():
    # Covered pages stay mapped (page_stack), so the film must ask whether its page is on top.
    t=driver(); page=SimpleNamespace(_page_hidden=True,master=None); t.canvas.master=page
    t.play(result())
    assert t.pending and not t.canvas.callbacks
    page._page_hidden=False; t.page_shown()
    assert t.running and len(t.canvas.callbacks)==1
    t.page_hidden()
    assert not t.running and not t.canvas.callbacks


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
    assert renderer.origin==(renderer.title_at[0],renderer.title_at[1]+renderer.line_offsets[-1]+22*s)


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


def width(text):
    return 10*len(text)


def test_title_that_fits_stays_one_line():
    assert dt.wrap_title('A real task',width,200)==('A real task',)


def test_long_title_wraps_into_two_balanced_lines_at_word_boundaries():
    lines=dt.wrap_title('Refactor payment retries with integration tests',width,300)
    assert lines==('Refactor payment retries','with integration tests')
    assert ' '.join(lines)=='Refactor payment retries with integration tests'


def test_overflowing_title_keeps_a_full_first_line_and_leaves_the_rest_for_the_ellipsis():
    first,second=dt.wrap_title('one two three four five six seven eight nine ten',width,150)
    assert first=='one two three' and width(first)<=150
    assert second.startswith('four') and width(second)>150
    # A single unbreakable word cannot wrap; its tile ellipsizes it on one line.
    assert dt.wrap_title('x'*40,width,150)==('x'*40,)


def test_cjk_breaks_between_characters_but_never_before_closing_punctuation():
    lines=dt.wrap_title('重构支付服务的插件架构，跨多个模块',width,100)
    assert len(lines)==2 and ''.join(lines)=='重构支付服务的插件架构，跨多个模块'
    assert all(width(line)<=100 for line in lines)
    assert not lines[1].startswith('，')


def test_two_line_title_grows_upward_and_everything_follows_the_block():
    one=dt.TheatreRenderer(dt.scene_from_result(result()),474,488,1.15)
    long=result(); long['task']['objective']='Refactor payment retries with integration tests and rollback'
    two=dt.TheatreRenderer(dt.scene_from_result(long),474,488,1.15)
    assert one.title_lines==('A real task',) and one.line_offsets==(0.,)
    assert len(two.title_lines)==2
    # The underline, node, kind and routes keep their place; the block's last line sits where one line did.
    assert two.origin==one.origin and two.kind_at==one.kind_at
    assert two.title_at[1]+two.line_offsets[-1]==one.title_at[1]
    calls=[]
    two.blit=lambda image,value,*a,**k: calls.append((value,a[:2]))
    two.frame(1.)
    ys={value:xy[1] for value,xy in calls if value in two.title_lines}
    assert set(ys)==set(two.title_lines) and ys[two.title_lines[0]]<ys[two.title_lines[1]]
    # Ink specks are seeded from both lines.
    seeds=[seed for seed,*_ in two.ink]
    assert min(y for _,y in seeds)<two.title_at[1]<max(y for _,y in seeds)


def fitted(source, targets=(.3, .8, .8, .3)):
    """The same decision with the task's need recorded, and two models that differ on every axis."""
    source = copy.deepcopy(source)
    scores = {'alpha': (.7, .85, .9, .6), 'beta': (.95, .7, .75, .9), 'blocked': (.5, .5, .5, .5)}
    keys = ('divergence', 'global_rigor', 'module_precision', 'vision_tools')
    for item in source['ranking']:
        values = scores[item['bundle']['model']]
        item['components'].update(dict(zip(('divergence', 'global_rigor', 'module_precision', 'capability'), values)))
        item['five_axis_fit'] = {key: dict(demand=1, score=value, target=target)
                                 for key, value, target in zip(keys, values, targets)}
    return source


def test_the_task_s_need_comes_from_the_snapshot_and_is_never_invented():
    assert dt.scene_from_result(result()).need == ()
    scene = dt.scene_from_result(fitted(result()))
    # One target per radar axis, in the radar's order; quota economy has none.
    assert scene.need == (.3, .8, .8, .3, None)
    broken = fitted(result())
    for item in broken['ranking']:
        item['five_axis_fit']['vision_tools'] = 'n/a'
        item['five_axis_fit']['divergence']['target'] = float('nan')
    assert dt.scene_from_result(broken).need == (None, .8, .8, None, None)
    # A renderer without a recorded need draws the shapes whole and no outline.
    plain = dt.TheatreRenderer(dt.scene_from_result(result()), 630, 600, 1.5)
    assert plain.need == () and plain.frame(dt.DURATION).size == (630, 600)


def test_shapes_bloom_from_the_centre_without_overshoot_and_the_winner_lands_last():
    renderer = dt.TheatreRenderer(dt.scene_from_result(fitted(result())), 630, 600, 1.5)
    times = [i / 120 for i in range(round(dt.DURATION * 120) + 1)]
    winner, other = ([renderer.bloom(index, t) for t in times] for index in (0, 1))
    for growth in (winner, other):
        assert growth[0] == (0,) * 5 and growth[-1] == (1,) * 5
        for axis in range(5):
            steps = [g[axis] for g in growth]
            # Each axis eases out to exactly its value: never beyond it, never back.
            assert steps == sorted(steps) and max(steps) == 1
            assert max(b - a for a, b in zip(steps, steps[1:])) < .06
        # The shape opens clockwise from the top axis.
        started = [next(i for i, g in enumerate(growth) if g[axis] > 0) for axis in range(5)]
        assert started == sorted(started) and len(set(started)) == 5
    landed = lambda growth: next(t for t, g in zip(times, growth) if g == (1,) * 5)
    opened = lambda growth: next(t for t, g in zip(times, growth) if g[0] > 0)
    assert opened(other) < opened(winner) and landed(other) < landed(winner) < dt.WINDOWS['settle'][0]
    # Both route ends have landed on the centre before the first shape opens there.
    assert dt.WINDOWS['lift'][1] <= opened(other)
    for candidate in renderer.scene.pair:
        path, lengths = renderer.pens[candidate.id]
        assert path[-1] == pytest.approx(renderer.center)


def test_the_need_is_a_dashed_line_and_ability_beyond_it_is_a_segment_along_its_axis():
    source = fitted(result())
    renderer = dt.TheatreRenderer(dt.scene_from_result(source), 630, 600, 1.5)
    whole = dt.TheatreRenderer(dt.scene_from_result(result() | {'ranking': [
        {k: v for k, v in item.items() if k != 'five_axis_fit'} for item in source['ranking']]}), 630, 600, 1.5)
    frame, flat = renderer.frame(dt.DURATION), whole.frame(dt.DURATION)
    cam = dt.camera_position(dt.DURATION, renderer.w)
    cx, cy, radius = renderer.center[0] - cam, renderer.center[1], renderer.radius
    spot = lambda axis, level, side=0.: tuple(round(v) for v in dt.radar.axis_segment(
        cx, cy, radius, axis, 5, level, level, side * renderer.s)[0])
    ink = lambda image, xy: sum(image.getpixel(xy))        # lower is more ink
    paper = sum(renderer.blank.reduce(renderer.k).getpixel((0, 0)))
    # A shape is one flat, very light tint: the same near the centre and far from it, with or without a need.
    for image in (frame, flat):
        assert image.getpixel(spot(0, .2, 7)) == image.getpixel(spot(0, .55, 7))
        assert ink(image, spot(0, .55, 7)) < paper - 6
    # Divergence: the need is .3 and both models go beyond it. Each has its segment on its own side of the spoke,
    # from the target outwards; inside the target, and without a recorded need, there is none.
    for style in dt.radar.SERIES_STYLES:
        side = dt.radar.MARK_SHIFT * style['side']
        assert ink(frame, spot(0, .5, side)) < ink(flat, spot(0, .5, side)) - 25
        assert frame.getpixel(spot(0, .15, side)) == flat.getpixel(spot(0, .15, side))
    # The need itself: a dot on each axis that has a target, joined by a dashed line; none on quota economy.
    darkest = lambda image, xy: min(sum(p) for _, p in image.crop((xy[0] - 2, xy[1] - 2, xy[0] + 3, xy[1] + 3)).getcolors(64))
    for axis, target in enumerate(renderer.need[:4]):
        assert darkest(frame, spot(axis, target)) < darkest(flat, spot(axis, target)) - 60, axis
    a, b = (dt.radar.point(cx, cy, radius * renderer.need[axis], axis, 5) for axis in (1, 2))
    along = [(round(a[0] + (b[0] - a[0]) * u / 60), round(a[1] + (b[1] - a[1]) * u / 60)) for u in range(8, 53)]
    dark = [ink(frame, xy) < ink(flat, xy) - 60 for xy in along]
    assert any(dark) and not all(dark)                       # dashes and gaps


def test_quota_cost_is_a_red_segment_from_the_point_to_full_scale_on_the_economy_axis():
    source = fitted(result())
    for item in source['ranking']:
        if item['bundle']['model'] == 'beta':
            item['components']['opportunity_cost'] = .06        # quota economy 1 − 5 × .06 = .7
    renderer = dt.TheatreRenderer(dt.scene_from_result(source), 630, 600, 1.5)
    cam = dt.camera_position(dt.DURATION, renderer.w)
    cx, cy, radius = renderer.center[0] - cam, renderer.center[1], renderer.radius
    frame = renderer.frame(dt.DURATION)
    for index, candidate in enumerate(renderer.scene.pair):
        value = candidate.values[4]
        side = dt.radar.MARK_SHIFT * dt.radar.SERIES_STYLES[index]['side'] * renderer.s
        at = lambda level: tuple(round(v) for v in dt.radar.axis_segment(cx, cy, radius, 4, 5, level, level, side)[0])
        if value < .9:
            red, green, blue = frame.getpixel(at((value + 1) / 2 - .02))    # clear of the vertex markers
            assert red > green + 50 and red > blue + 50, candidate.name
    assert any(candidate.values[4] < .9 for candidate in renderer.scene.pair)
