"""A small, deterministic decision film. PIL renders off-screen; Tk only owns its clock.

The scene deliberately contains no score/cost/caption text: order, light and geometry
carry the comparison. Full evidence remains in the panel's candidate table.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from functools import lru_cache
import math
import re
import time

from PIL import Image, ImageDraw, ImageFont, ImageColor, ImageTk

import radar_chart as radar
import ui_theme as theme


FPS = 60
FRAME = 1 / FPS
# The task phrase is fully readable and still for TITLE_HOLD - TITLE_IN seconds.
TITLE_IN, TITLE_HOLD = .35, 1.75
DURATION = 9.9
BEATS = (("task", 0., TITLE_HOLD), ("kind", TITLE_HOLD, 2.85), ("open", 2.85, 4.4),
         ("rank", 4.4, 5.4), ("compare", 5.4, 7.8), ("settle", 7.8, DURATION))
# Overlapping windows on one clock; each later object starts from the previous one.
# The kind resolves under the condensing underline (classify) and both stay readable
# until the gather; "routes" opens when the first ink speck leaves the node.
WINDOWS = {"appear": (0., TITLE_IN), "underline": (.1, .55), "classify": (TITLE_HOLD, 2.3),
           "gather": (2.85, 3.6), "routes": (3.35, 4.8), "rank": (4.4, 5.4),
           "dissolve": (5.3, 6.0), "pen": (5.4, 7.3), "glide": (5.5, 7.2), "grid": (6.1, 7.3),
           "fill": (7.05, 7.5), "fold": (7.8, 9.1), "effort": (8.35, 9.25)}
HEAD_TRAVEL = .55       # seconds a leading speck flies from its glyph into the node
ROUTE_SPAN = 1.2        # seconds a route head takes from the node to its model
# Camera: still through the title hold and the kind, then one rightward pan whose
# smoothstep velocity ramp is still near zero while the ink gathers and gains speed
# as the routes leave the node; settled before the fold.
PAN_START, PAN_END, PAN_RAMP = WINDOWS["gather"][0], 7.8, 1.5
CAMERA_SPAN = 1.15      # pan distance in stage widths
SUPERSAMPLE = 2         # every moving object is rasterized at 2x and box-filtered


def beat_at(seconds):
    for name, start, end in BEATS:
        if seconds < end:
            return name, radar.clamp((seconds - start) / (end - start))
    return "settle", 1.


def ease(t):
    t = radar.clamp(t)
    return t * t * (3 - 2 * t)


def window(seconds, name):
    start, end = WINDOWS[name]
    return radar.clamp((seconds - start) / (end - start))


def _ramp(u):
    """Integral of smoothstep on [0, 1]; equals .5 at u = 1."""
    u = radar.clamp(u)
    return u ** 3 - u ** 4 / 2


def camera_position(seconds, width):
    """World x of the viewport's left edge: one C1 (indeed C2) curve, never beatwise."""
    total = CAMERA_SPAN * width
    span, ramp = PAN_END - PAN_START, PAN_RAMP
    speed = total / (span - ramp)
    t = seconds - PAN_START
    if t <= 0:
        return 0.
    if t >= span:
        return float(total)
    if t < ramp:
        return speed * ramp * _ramp(t / ramp)
    if t > span - ramp:
        return total - speed * ramp * _ramp((span - t) / ramp)
    return speed * ramp * .5 + speed * (t - ramp)


def frame_time(elapsed):
    """Content time for a tick: the latest frame deadline already reached.

    Content advances by exact multiples of FRAME, so a late timer callback changes
    when a frame is shown but never the displacement between consecutive frames.
    """
    # A 2 ms tolerance absorbs a callback firing marginally before its deadline.
    return min(DURATION, math.floor((max(0., elapsed) + .002) / FRAME) * FRAME)


def next_delay_ms(started, now, frame_index):
    """Milliseconds until the deadline of frame `frame_index`, never early."""
    return max(1, math.ceil((started + frame_index * FRAME - now) * 1000))


def effort_label(effort):
    if not effort:
        return ""
    from decision_summary import EFFORTS
    from i18n import translate
    return "· " + translate(EFFORTS.get(effort, effort))


class TimerResolution:
    """Raise the Windows timer resolution to 1 ms only while a film is playing."""

    def __init__(self):
        self.active = False

    def begin(self):
        if self.active:
            return
        try:
            import ctypes
            self.active = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except (AttributeError, OSError):
            self.active = False

    def end(self):
        if not self.active:
            return
        self.active = False
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except (AttributeError, OSError):
            pass


def short_title(title):
    """Keep a real phrase from the objective, never generate a new task name."""
    title=re.sub(r'^(?:执行)?任务(?:（[^）]*）)?[：:]\s*','',title)
    quoted=re.search(r'「([^」]+)」',title)
    if quoted:
        return quoted[1]
    return re.split(r'[，；。]',title,maxsplit=1)[0]


def sampled_polygon(values,cx,cy,radius,count=65):
    vertices=radar.polygon(list(values),cx,cy,radius)
    points=[]
    for i in range(count):
        pos=i/(count-1)*len(vertices)
        segment=min(len(vertices)-1,int(pos)); t=pos-segment
        a,b=vertices[segment],vertices[(segment+1)%len(vertices)]
        points.append((a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t))
    return tuple(points)


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def snapshot_result(row):
    """Single schema boundary: an allocator-shaped `decision_snapshot` (or flat plan).

    Returns None for legacy records, never invents missing ranking or components.
    A snapshot may wrap task metadata in `task`; ranking/bundle fields retain the
    allocator's shape. Backend aliases are intentionally limited to `snapshot`.
    """
    snapshot = row.get("decision_snapshot", row.get("snapshot"))
    if snapshot is None and isinstance(row.get("ranking"), list):
        snapshot = row
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("ranking"), list):
        return None
    metadata = {k: row[k] for k in ("objective", "work_kind", "kind", "profile", "at") if k in row}
    return {**metadata, **snapshot, "theatre_source": "snapshot"}


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    score: float | None
    values: tuple
    excluded: tuple
    effort: str = ""


@dataclass(frozen=True)
class Scene:
    title: str
    kind: str
    candidates: tuple
    pair: tuple
    winner: Candidate | None
    recomputed: bool


def scene_from_result(result):
    import router_data
    task = result.get("task") or (result.get("decision") or {}).get("task") or {}
    if not isinstance(task, dict):
        task = {"objective": str(task)}
    title = str(task.get("objective") or result.get("objective") or task.get("title") or "")
    # A brief may be markdown. Its first nonempty line is its actual title, not a fabricated summary.
    title = next((s.strip().lstrip("# ") for s in title.splitlines() if s.strip()), "")
    profile = task.get("profile") or result.get("profile") or {}
    kind = task.get("work_kind") or task.get("kind") or result.get("work_kind") or result.get("kind") or profile.get("work_kind") or ""
    from decision_summary import kind_label
    kind = kind_label(kind) if kind else ""
    rows = []
    for item in result.get("ranking") or []:
        if not isinstance(item, dict):
            continue
        bundle = item.get("bundle") or {}
        comp = item.get("components") or {}
        # Never draw inferred zero axes for a record with no score components.
        values = tuple(radar.score_dimensions(comp).values()) if comp else ()
        score = item.get("score")
        rows.append(Candidate(str(item.get("id") or bundle.get("bundle_id") or ""),
                              router_data.model_label(str(bundle.get("model") or item.get("id") or "")),
                              score if finite(score) else None, values,
                              tuple(str(x) for x in item.get("excluded") or []),
                              str(bundle.get("effort") or item.get("effort") or "")))
    rows.sort(key=lambda c: (bool(c.excluded), c.score is None, -(c.score or 0)))
    choice = result.get("choice") or (result.get("selected_bundle") or {}).get("bundle_id")
    winner = next((c for c in rows if c.id == choice), None)
    eligible = [c for c in rows if not c.excluded and c.score is not None]
    # Compare models, as the existing compass does; several accounts/efforts of
    # one model must not masquerade as two different contenders.
    first = winner if winner in eligible else next(iter(eligible), None)
    second = next((c for c in eligible if first and c.name != first.name), None)
    pair = tuple(c for c in (first, second) if c and c.values)
    return Scene(title, kind, tuple(rows), pair, winner, result.get("theatre_source") == "recomputed")


def route_points(start, end, count=65):
    """An S route sampled once; equal-distance resampling keeps particles unhurried."""
    mx = (start[0] + end[0]) / 2
    controls = (start, (mx, start[1]), (mx, end[1]), end)
    raw = []
    for i in range(count):
        t = i / (count - 1)
        weights = ((1-t)**3, 3*t*(1-t)**2, 3*t*t*(1-t), t**3)
        raw.append(tuple(sum(p[k]*v for p, v in zip(controls, weights)) for k in (0, 1)))
    lengths = [0.]
    for a, b in zip(raw, raw[1:]):
        lengths.append(lengths[-1] + math.dist(a, b))
    out, j = [], 1
    for i in range(count):
        distance = lengths[-1] * i / (count-1)
        while j < count-1 and lengths[j] < distance:
            j += 1
        part = (distance-lengths[j-1]) / max(1e-9, lengths[j]-lengths[j-1])
        out.append(tuple(raw[j-1][k]+(raw[j][k]-raw[j-1][k])*part for k in (0, 1)))
    return tuple(out)


def shown_candidates(scene, count=5):
    rows, names = [], set()
    for candidate in scene.candidates:
        if candidate.name not in names:
            rows.append(candidate)
            names.add(candidate.name)
        if len(rows) == count:
            break
    # Include one excluded route when present and the actual selected bundle.
    for candidate in (next((c for c in scene.candidates if c.excluded), None), scene.winner):
        if candidate and candidate.name not in {c.name for c in rows}:
            if len(rows) == count:
                rows[-1 if candidate.excluded else -2] = candidate
            else:
                rows.append(candidate)
    return tuple(rows)


@lru_cache(maxsize=48)
def font(size, bold=False):
    names = ("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
             "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, max(8, size))
        except OSError:
            pass
    return ImageFont.load_default()


def hash01(i):
    value = math.sin(i * 12.9898 + 78.233) * 43758.5453
    return value - math.floor(value)


def lerp(a, b, t):
    return a + (b - a) * t


def lerp2(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def bezier2(a, control, b, count=40):
    return tuple(lerp2(lerp2(a, control, i/(count-1)), lerp2(control, b, i/(count-1)), i/(count-1))
                 for i in range(count))


def arc_lengths(points):
    out = [0.]
    for a, b in zip(points, points[1:]):
        out.append(out[-1] + math.dist(a, b))
    return out


def partial(points, lengths, distance):
    """Polyline prefix up to an arc length, ending on an interpolated (sub-sample) point."""
    distance = max(0., min(lengths[-1], distance))
    j = max(1, bisect.bisect_left(lengths, distance))
    a, b = points[j-1], points[j]
    part = (distance - lengths[j-1]) / max(1e-9, lengths[j] - lengths[j-1])
    return list(points[:j]) + [lerp2(a, b, part)]


def fraction_prefix(points, reveal):
    """Prefix of an equal-distance sampled path with a fractional end."""
    position = (len(points) - 1) * radar.clamp(reveal)
    j = int(position)
    if j >= len(points) - 1:
        return list(points)
    return list(points[:j+1]) + [lerp2(points[j], points[j+1], position - j)]


class TheatreRenderer:
    """Physical-pixel renderer.

    Every moving object is drawn at float positions into a 2x buffer and box-filtered,
    text is placed and scaled by a fractional affine transform from one cached tile,
    so neither the pan nor a scale ever steps by whole pixels or font sizes.
    """

    def __init__(self, scene, width, height, scale=1.):
        self.scene = scene
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self.s = min(scale, self.w / 310, self.h / 300)
        self.k = SUPERSAMPLE
        self.rows = shown_candidates(scene)
        w, h, s = self.w, self.h, self.s
        self.title = short_title(scene.title)
        self.title_at = (w*.5, h*.44)
        # The underline's centre is the route origin; the kind sits below with a clear gap.
        self.origin = (w*.5, h*.44 + 22*s)
        self.kind_at = (w*.5, self.origin[1] + 19*s)
        self.end_x = w*.92
        self.radius = min(w*.245, h*.25)
        # The radar (and the winner's name) sit where the settled camera centres them.
        self.center = (w*(CAMERA_SPAN+.5), h*.44)
        self.final_y = h*.5
        self.legend_y = [self.center[1]+self.radius+(20+23*i)*s for i in range(2)]
        self.targets = self.row_positions()
        self.starts = [y+(12 if i % 2 else -12)*s for i, y in enumerate(self.targets)]
        # Cached modern paper: less than 1% warm-neutral tint, no decorative pattern.
        self.blank = Image.new("RGB", (w*self.k, h*self.k), "#FEFEFD")
        self.text_cache = {}
        self.tiles = {}
        self.pair_index = {c.name: i for i, c in enumerate(scene.pair)}
        self.vertices = {c.id: radar.polygon(list(c.values), *self.center, self.radius) for c in scene.pair}
        self.pens = {}
        for candidate in scene.pair:
            index = next((i for i, c in enumerate(self.rows) if c.name == candidate.name), None)
            if index is None:
                continue
            end = (self.end_x, self.targets[index])
            top = self.vertices[candidate.id][0]
            control = (lerp(end[0], top[0], .55), min(end[1], top[1]) - h*.08)
            lift = bezier2(end, control, top)
            # One pen path: the route end lifts to the first vertex and traces the polygon.
            path = list(lift) + list(sampled_polygon(candidate.values, *self.center, self.radius, 81)[1:])
            self.pens[candidate.id] = (tuple(path), arc_lengths(path), arc_lengths(lift)[-1])
        self.final = self.final_layout()
        self.seeds = self.speck_seeds()
        self.ink, self.heads = self.ink_specks()
        self.probe = {}

    def row_positions(self):
        n = len(self.rows)
        return [self.h*(.20 + .60*i/max(1, n-1)) for i in range(n)]

    # --- typography -------------------------------------------------------------------
    def text(self, image, value, xy, size=13, color=theme.INK, bold=False, anchor="mm", max_width=None, alpha=1.):
        """Static 1x text for the verdict strip (never animated)."""
        f = font(round(size*self.s), bold)
        limit = int(max_width or self.w*.88)
        key = (value, size, color, bold, limit)
        if key not in self.text_cache:
            text = str(value)
            if f.getlength(text) > limit:
                while text and f.getlength(text + "…") > limit:
                    text = text[:-1]
                text += "…"
            box = f.getbbox(text or " ")
            tile = Image.new("RGBA", (max(1, box[2]-box[0]+4), max(1, box[3]-box[1]+4)))
            ImageDraw.Draw(tile).text((2-box[0], 2-box[1]), text, font=f, fill=color)
            self.text_cache[key] = tile
        tile = self.text_cache[key]
        if alpha < 1:
            tile = tile.copy()
            tile.putalpha(tile.getchannel("A").point(lambda a: round(a*radar.clamp(alpha))))
        x, y = xy
        x -= tile.width/2 if anchor == "mm" else 0
        image.paste(tile, (round(x), round(y-tile.height/2)), tile)
        return tile, (round(x), round(y-tile.height/2))

    def tile(self, value, size, color, bold=False, max_width=None, half=False):
        """One cached supersampled tile per string; a centred vertical metric box, so
        strings of one font share a midline. `half` is a pre-filtered half-size mip."""
        key = (value, size, color, bold, None if max_width is None else round(max_width), half)
        if key in self.tiles:
            return self.tiles[key]
        if half:
            full = self.tile(value, size, color, bold, max_width)
            out = full.resize((max(1, full.width//2), max(1, full.height//2)), Image.Resampling.LANCZOS)
        else:
            f = font(round(size*self.s*self.k), bold)
            text = str(value)
            limit = (max_width or self.w*.88) * self.k
            if f.getlength(text) > limit:
                while text and f.getlength(text + "…") > limit:
                    text = text[:-1]
                text += "…"
            ascent, descent = f.getmetrics()
            box = f.getbbox(text or " ", anchor="lm")
            rgb = ImageColor.getrgb(color)
            # Transparent pixels carry the ink colour, so filtering never darkens edges.
            out = Image.new("RGBA", (max(1, box[2]-box[0]+4), ascent+descent+4), (*rgb, 0))
            ImageDraw.Draw(out).text((2-box[0], 2+(ascent+descent)/2), text, font=f, fill=(*rgb, 255), anchor="lm")
        self.tiles[key] = out
        return out

    def measure(self, value, size, bold=False, max_width=None):
        return self.tile(value, size, theme.INK, bold, max_width).width / self.k

    def blit(self, image, value, x, y, size=13, color=theme.INK, bold=False, scale=1., alpha=1.,
             anchor="mm", max_width=None, cam=0.):
        """Place text at a float screen position and fractional scale about its anchor."""
        alpha = radar.clamp(alpha)
        if alpha <= .004 or scale <= .02:
            return
        half = scale < .7
        tile = self.tile(value, size, color, bold, max_width, half)
        factor = scale * (2 if half else 1)
        k = self.k
        tw, th = tile.size
        left = (x - cam) * k - (tw*factor/2 if anchor == "mm" else 0)
        top = y * k - th*factor/2
        ox, oy = math.floor(left), math.floor(top)
        size = (math.ceil(left + tw*factor) - ox + 1, math.ceil(top + th*factor) - oy + 1)
        if ox >= image.width or oy >= image.height or ox + size[0] <= 0 or oy + size[1] <= 0:
            return
        out = tile.transform(size, Image.Transform.AFFINE,
                             (1/factor, 0, (ox-left)/factor, 0, 1/factor, (oy-top)/factor),
                             resample=Image.Resampling.BICUBIC)
        if alpha < 1:
            out.putalpha(out.getchannel("A").point(lambda a: round(a*alpha)))
        image.paste(out, (ox, oy), out)

    # --- geometry helpers ---------------------------------------------------------------
    def screen(self, points, cam):
        k = self.k
        return [((x-cam)*k, y*k) for x, y in points]

    def line(self, draw, points, cam, color, width=1, dashed=False):
        points = self.screen(points, cam)
        if len(points) < 2 or color[3] <= 0:
            return
        size = max(1, round(width*self.s*self.k))
        halo = (*color[:3], min(15, color[3]//10))
        if dashed:
            for i in range(0, len(points)-1, 4):
                draw.line(points[i:i+3], fill=halo, width=size+2*self.k)
                draw.line(points[i:i+3], fill=color, width=size)
        else:
            draw.line(points, fill=halo, width=size+2*self.k, joint="curve")
            draw.line(points, fill=color, width=size, joint="curve")

    def dot(self, draw, point, cam, radius, fill, outline=None):
        x, y = (point[0]-cam)*self.k, point[1]*self.k
        r = radius*self.s*self.k
        draw.ellipse((x-r, y-r, x+r, y+r), fill=fill, outline=outline,
                     width=max(1, round(self.s*self.k*.8)) if outline else 0)

    def final_layout(self):
        winner = self.scene.winner
        if not winner:
            return None
        effort = effort_label(winner.effort)
        name_w = self.measure(winner.name, 25, True, self.w*.62)
        effort_w = self.measure(effort, 25, False) * .76 if effort else 0.
        gap = 7*self.s if effort else 0.
        left = self.center[0] - (name_w + gap + effort_w)/2
        name_center = (left + name_w/2, self.final_y)
        effort_center = (left + name_w + gap + effort_w/2, self.final_y)
        targets = [(left + name_w*f, self.final_y) for f in (.12, .38, .62, .88)]
        targets.append(effort_center if effort else (left + name_w*.97, self.final_y))
        mapping = {}
        if winner.id in self.vertices:
            # Vertices travel into the letters left to right, so no two paths cross.
            order = sorted(range(5), key=lambda i: self.vertices[winner.id][i][0])
            mapping = {vertex: targets[rank] for rank, vertex in enumerate(order)}
        return dict(effort=effort, name_center=name_center, effort_center=effort_center, targets=mapping)

    def speck_seeds(self):
        """Deterministic ink specks: losing labels and the runner dissolve into them."""
        seeds, s, n = [], self.s, 0
        start = WINDOWS["dissolve"][0]
        for i, row in enumerate(self.rows):
            if row.name in self.pair_index:
                continue
            width = min(self.measure(row.name, 11, False, self.w*.47), self.w*.47)
            for j in range(4):
                seeds.append((start + i*.04 + j*.06, self.end_x + 6*s + width*(j+.5)/4,
                              self.targets[i] + (hash01(n)-.5)*6*s, theme.FAINT, n))
                n += 1
        if len(self.scene.pair) > 1 and self.scene.pair[1].id in self.vertices:
            runner = self.scene.pair[1]
            fold = WINDOWS["fold"][0]
            width = self.measure(runner.name, 13, True)
            for j in range(5):
                seeds.append((fold + j*.05, self.center[0] + width*((j+.5)/5-.5),
                              self.legend_y[1] + (hash01(n)-.5)*6*s, theme.CYAN, n))
                n += 1
            for j, vertex in enumerate(self.vertices[runner.id]):
                seeds.append((fold + .12 + j*.04, *vertex, theme.CYAN, n))
                n += 1
        return tuple(seeds[:24])

    # --- frame ----------------------------------------------------------------------------
    def frame(self, seconds):
        # All objects share this continuous clock; beats overlap, none is a separate slide.
        t = seconds
        cam = camera_position(t, self.w)
        image = self.blank.copy()
        draw = ImageDraw.Draw(image, "RGBA")
        self.probe = {"t": t, "camera": cam}
        self.intro(image, draw, t, cam)
        self.grid(draw, t, cam)
        states = self.flow(draw, t, cam)
        self.polygons(draw, t, cam)
        self.labels(image, t, cam, states)
        self.specks(draw, t, cam)
        image = image.reduce(self.k)
        self.dots(image, t)
        return image

    def glyph_points(self, value, size, color, center):
        """World positions of inked pixels of a cached text tile (every 3rd 2x pixel)."""
        if not value:
            return []
        tile = self.tile(value, size, color, False, self.w*.88)
        data, k = tile.getchannel("A").load(), self.k
        return [(center[0] + (x - tile.width/2)/k, center[1] + (y - tile.height/2)/k)
                for y in range(0, tile.height, 3) for x in range(0, tile.width, 3) if data[x, y] > 170]

    def ink_specks(self):
        """Title/kind glyphs break into specks on curved paths into the node; one speck
        per route keeps its momentum through the node and leads that route."""
        s, w = self.s, self.w
        ox, oy = self.origin
        title = self.glyph_points(self.title, 17, theme.INK, self.title_at)
        kind = self.glyph_points(self.scene.kind, 12, theme.MUTED, self.kind_at)

        def pick(points, count, salt):
            order = sorted(range(len(points)), key=lambda i: hash01(i*7.13 + salt))
            return [points[i] for i in order[:count]]
        ink = []
        for n, seed in enumerate(pick(title, 22, 1) + pick(kind, 8, 2)):
            bend = (1 if n % 2 else -1) * (.18 + .12*hash01(n+37))
            mid = lerp2(seed, self.origin, .5)
            control = (mid[0] - (oy-seed[1])*bend, mid[1] + (ox-seed[0])*bend)
            ink.append((seed, control, WINDOWS["gather"][0] + .3*hash01(n+11), .42 + .14*hash01(n+23), n))
        heads = []
        left = sorted(p for p in title if p[0] < ox - w*.05) or [(ox - w*.2, oy - 20*s)]
        self.route_timing = []
        for i in range(len(self.rows)):
            seed = left[int(len(left) * i / max(1, len(self.rows)))]
            # Horizontal arrival, so the speck leaves along the route's horizontal tangent.
            control = (ox - max(30*s, (ox - seed[0])*.6), oy)
            path = bezier2(seed, control, self.origin, 24)
            lengths = arc_lengths(path)
            route = arc_lengths(route_points(self.origin, (self.end_x, self.starts[i])))[-1]
            # Matched speed at the node: the route head starts at L'/ROUTE_SPAN, so the
            # speck's Hermite end slope is chosen to arrive at exactly that speed.
            slope = min(3., route / ROUTE_SPAN * HEAD_TRAVEL / max(1e-6, lengths[-1]))
            arrive = WINDOWS["routes"][0] + i*.07
            heads.append((path, lengths, arrive, slope))
            self.route_timing.append((arrive, ROUTE_SPAN))
        return tuple(ink), tuple(heads)

    def reveal(self, index, t):
        arrive, span = self.route_timing[index]
        u = radar.clamp((t - arrive) / span)
        # Hermite: unit start slope (mean speed, the arriving speck's speed), zero end slope.
        return u + u*u - u**3

    def node_growth(self, t):
        """The node grows from the specks merging into it."""
        if not self.ink:
            return ease(window(t, "gather"))
        return sum(ease((t - start - duration*.8) / (duration*.25))
                   for _, _, start, duration, _ in self.ink) / len(self.ink)

    def row_color(self, row):
        pair = self.pair_index.get(row.name)
        if pair is None or self.scene.pair[pair].id not in self.pens:
            return theme.FAINT
        return theme.ACCENT if pair == 0 else theme.CYAN

    def intro(self, image, draw, t, cam):
        s, w = self.s, self.w
        appear, under = ease(window(t, "appear")), ease(window(t, "underline"))
        kind_in, g = ease(window(t, "classify")), window(t, "gather")
        tx, ty = self.title_at
        ox, oy = self.origin
        if g < 1:
            # The title recedes a little as the kind gains, then breaks into ink.
            alpha = appear * (1 - .28*kind_in) * (1 - ease(g/.45))
            self.blit(image, self.title, tx, ty, 17, theme.INK, scale=1., alpha=alpha, max_width=w*.88, cam=cam)
            if alpha > 0:
                self.probe["title"] = (tx - cam, ty, 1.)
            # The underline condenses toward its own centre, the node's position.
            half = w * (.14*under - .095*kind_in) * (1 - ease(g/.5))
            if half > .3:
                draw.line(self.screen(((ox - half, oy), (ox + half, oy)), cam),
                          fill=(*ImageColor.getrgb(theme.ACCENT), round((135 + 70*kind_in)*appear)),
                          width=max(1, round((.9 + .5*kind_in)*s*self.k)))
            if self.scene.kind:
                kx, ky = self.kind_at
                self.blit(image, self.scene.kind, kx, ky - 5*s*(1 - kind_in), 12, theme.MUTED,
                          scale=.9 + .1*kind_in, alpha=kind_in*(1 - ease((g - .05)/.45)), cam=cam)
        ink, accent = ImageColor.getrgb(theme.INK), ImageColor.getrgb(theme.ACCENT)
        for seed, control, start, duration, n in self.ink:
            u = (t - start) / duration
            if not 0 < u < 1:
                continue
            p = ease(u)
            point = lerp2(lerp2(seed, control, p), lerp2(control, self.origin, p), p)
            colour = tuple(round(lerp(a, b, p)) for a, b in zip(ink, accent))
            alpha = 175 * min(1., u*6) * (1 - ease((u - .8)/.2))
            self.dot(draw, point, cam, .9 + .45*hash01(n+5), (*colour, round(alpha)))
        for row, (path, lengths, arrive, slope) in zip(self.rows, self.heads):
            u = (t - arrive + HEAD_TRAVEL) / HEAD_TRAVEL
            # Drawn through u == 1, the frame where its route has not yet begun.
            if not 0 < u <= 1:
                continue
            # Hermite from rest to the end slope that matches the route head's speed.
            progress = 3*u*u - 2*u**3 + slope*(u**3 - u*u)
            point = partial(path, lengths, lengths[-1]*progress)[-1]
            target = ImageColor.getrgb(self.row_color(row))
            colour = tuple(round(lerp(a, b, u)) for a, b in zip(ink, target))
            self.dot(draw, point, cam, 1.1 + 1.3*u, (*colour, round(240*min(1., u*5))))
            if row is self.rows[0]:
                self.probe["head"] = (point[0]-cam, point[1])

    def grid(self, draw, t, cam):
        shown = ease(window(t, "grid")) * (1 - ease(window(t, "fold")/.55))
        if shown <= 0 or not self.scene.pair:
            return
        cx, cy = self.center
        radius = self.radius * (.88 + .12*ease(window(t, "grid")))
        outer = self.screen(radar.polygon([1]*5, cx, cy, radius), cam)
        draw.polygon(outer, fill=(248, 250, 252, round(255*shown)))
        for level in (.25, .5, .75, 1):
            ring = self.screen(radar.polygon([level]*5, cx, cy, radius), cam)
            color = ImageColor.getrgb("#E2E8F0" if level < 1 else "#CBD5E1")
            draw.line(ring + ring[:1], fill=(*color, round(255*shown)),
                      width=max(1, round((.75 if level < 1 else 1)*self.s*self.k)), joint="curve")
        centre = self.screen(((cx, cy),), cam)[0]
        for vertex in outer:
            draw.line((centre, vertex), fill=(226, 232, 240, round(255*shown)), width=max(1, round(.75*self.s*self.k)))

    def pen(self, candidate, t):
        path, lengths, lift = self.pens[candidate.id]
        return path, lengths, lift, ease(window(t, "pen")) * lengths[-1]

    def flow(self, draw, t, cam):
        rank, gone = ease(window(t, "rank")), ease(window(t, "dissolve"))
        detach = ease(window(t, "grid"))
        states = []
        grow = self.node_growth(t)
        if grow > 0 and detach < 1:
            fill = (*ImageColor.getrgb(theme.CARD), round(255*min(1., grow*3)*(1-detach)))
            self.dot(draw, self.origin, cam, 1 + 4*grow, fill,
                     (*ImageColor.getrgb(theme.ACCENT), round(255*min(1., grow*2)*(1-detach))))
        for i, row in enumerate(self.rows):
            y = lerp(self.starts[i], self.targets[i], rank)
            pair = self.pair_index.get(row.name)
            leader = pair is not None and self.scene.pair[pair].id in self.pens
            reveal = self.reveal(i, t)
            end = (self.end_x, y)
            if leader:
                path, lengths, lift, travelled = self.pen(self.scene.pair[pair], t)
                if travelled > 0:
                    end = partial(path, lengths, min(travelled, lift))[-1]
                opacity, width = 1 - detach, 1 + 1.2*rank
                color = theme.ACCENT if pair == 0 else theme.CYAN
            else:
                opacity = (.24 if row.excluded else 1 - .6*rank) * (1 - gone)
                width, color = 1, theme.FAINT
            states.append((y, end, reveal))
            if reveal <= 0 or opacity <= .003:
                continue
            route = route_points(self.origin, end)
            shown = fraction_prefix(route, reveal)
            rgb = ImageColor.getrgb(color)
            self.line(draw, shown, cam, (*rgb, round(210*opacity)), width,
                      self.scene.recomputed or bool(row.excluded))
            if reveal < 1:
                # A luminous head draws the path; the candidate resolves on arrival.
                self.dot(draw, shown[-1], cam, 2.4, (*rgb, round(240*opacity)))
                if i == 0:
                    self.probe["head"] = (shown[-1][0]-cam, shown[-1][1])
            elif not row.excluded:
                count = 3 if leader else 1
                fade = opacity * (1 - ease(window(t, "pen")) if leader else opacity)
                for j in range(count):
                    p = ((t - WINDOWS["routes"][0])*.40 + j/count) % 1
                    self.dot(draw, fraction_prefix(route, p)[-1], cam, 2.3 if leader else 1.4,
                             (*rgb, round(210*fade)))
            if pair == 0:
                self.probe["leader"] = (end[0]-cam, end[1])
        return states

    def polygons(self, draw, t, cam):
        fill = ease(window(t, "fill"))
        u = window(t, "fold")
        winner = self.scene.winner
        for index, candidate in enumerate(self.scene.pair):
            if candidate.id not in self.pens:
                continue
            path, lengths, lift, travelled = self.pen(candidate, t)
            if travelled <= 0:
                continue
            rgb = ImageColor.getrgb(theme.ACCENT if index == 0 else theme.CYAN)
            head = partial(path, lengths, travelled)[-1]
            if index == 0:
                self.probe["leader"] = (head[0]-cam, head[1])
            if 0 < window(t, "pen") < 1:
                self.dot(draw, head, cam, 2.4, (*rgb, 240))
            if travelled <= lift:
                continue
            vertices = self.vertices[candidate.id]
            dashed = index == 1
            if u <= 0:
                # Drop the lift (bezier2 samples 40 points); keep the traced perimeter.
                traced = partial(path, lengths, travelled)[39:]
                if travelled >= lengths[-1] - 1e-6 and fill > 0:
                    draw.polygon(self.screen(vertices, cam), fill=(*rgb, round((40 if index == 0 else 22)*fill)))
                self.line(draw, traced, cam, (*rgb, 235), 1.6, dashed)
                perimeter = lengths[-1] - lift
                for k, vertex in enumerate(vertices):
                    if travelled - lift >= perimeter*k/5 - 1e-6:
                        self.vertex(draw, vertex, cam, rgb, index, 1.)
                continue
            if winner and candidate.id == winner.id and self.final:
                moved = []
                for k, vertex in enumerate(vertices):
                    m = ease((u - k*.04) / .84)
                    moved.append(lerp2(vertex, self.final["targets"].get(k, vertex), m))
                ink = 1 - ease((u - .45) / .4)
                draw.polygon(self.screen(moved, cam), fill=(*rgb, round(40*(1-ease(u/.7)))))
                self.line(draw, moved + moved[:1], cam, (*rgb, round(235*ink)), 1.6)
                for vertex in moved:
                    self.vertex(draw, vertex, cam, rgb, index, ink)
                self.probe["fold_vertices"] = [(x-cam, y) for x, y in moved]
            else:
                # The runner contracts toward its name and dissolves into specks.
                q = ease(u / .5)
                anchor = (self.center[0], self.legend_y[min(index, 1)])
                moved = [lerp2(v, anchor, .55*q) for v in path[39:]]
                self.line(draw, moved, cam, (*rgb, round(235*(1-q))), 1.6, dashed)

    def vertex(self, draw, point, cam, rgb, index, alpha):
        if alpha <= 0:
            return
        if index == 0:
            self.dot(draw, point, cam, 2.3, (*rgb, round(255*alpha)))
        else:
            self.dot(draw, point, cam, 2.1, (255, 255, 255, round(255*alpha)), (*rgb, round(255*alpha)))

    def labels(self, image, t, cam, states):
        s, rank = self.s, ease(window(t, "rank"))
        gone, glide = ease(window(t, "dissolve")), ease(window(t, "glide"))
        u = window(t, "fold")
        winner = self.scene.winner
        for i, (row, (y, end, reveal)) in enumerate(zip(self.rows, states)):
            arrive = ease((reveal - .75) / .25)
            pair = self.pair_index.get(row.name)
            if pair is None or self.scene.pair[pair].id not in self.pens:
                alpha = arrive * (.24 if row.excluded else 1 - .5*rank) * (1 - gone)
                self.blit(image, row.name, self.end_x + 6*s, y, 11, theme.MUTED, anchor="lm",
                          max_width=self.w*.47, alpha=alpha, cam=cam)
                continue
            candidate = self.scene.pair[pair]
            color = theme.ACCENT if pair == 0 else theme.CYAN
            # One 25 px tile scaled continuously: route label → legend → the final name.
            width = self.measure(row.name, 25, True, self.w*.62) * 13/25
            start = (self.end_x + 6*s + width/2, y)
            # Labels follow their lifting route ends rightward, then settle under the radar.
            legend = (self.center[0], self.legend_y[pair])
            position = lerp2(lerp2(start, (legend[0], start[1]), glide), lerp2((legend[0], start[1]), legend, glide), glide)
            scale, alpha = 13/25, arrive
            if winner and candidate.id == winner.id and self.final:
                m = ease((u - .1) / .75)
                position = lerp2(position, self.final["name_center"], m)
                scale = lerp(13/25, 1, m)
                self.probe["winner_label"] = (position[0]-cam, position[1], scale)
            else:
                alpha *= 1 - ease(u / .4)
            self.blit(image, row.name, *position, 25, color, True, scale=scale, alpha=alpha,
                      max_width=self.w*.62, cam=cam)
        if not self.final:
            return
        if winner.id not in self.pens:
            # A selected bundle outside the comparison still lands as the final name.
            self.blit(image, winner.name, *self.final["name_center"], 25, theme.ACCENT, True,
                      alpha=ease((u - .3) / .5), max_width=self.w*.62, cam=cam)
        if self.final["effort"]:
            # The effort condenses where the last vertex lands, as part of the fold.
            e = ease(window(t, "effort"))
            self.blit(image, self.final["effort"], *self.final["effort_center"], 25, theme.ACCENT,
                      scale=.76*(.7 + .3*e), alpha=.7*e, cam=cam)

    def specks(self, draw, t, cam):
        life = .9
        for start, x, y, color, n in self.seeds:
            p = (t - start) / life
            if not 0 < p < 1:
                continue
            drift = 1 - (1 - p)**2
            px = x + (10 + 14*hash01(n+31))*self.s*life*drift
            py = y + (-7 + 14*hash01(n+57))*self.s*life*drift
            alpha = round(130 * (1-p)**1.5 * min(1., p*8))
            self.dot(draw, (px, py), cam, .7 + .5*hash01(n+83), (*ImageColor.getrgb(color), alpha))

    def dots(self, image, t):
        name, _ = beat_at(t)
        d = ImageDraw.Draw(image)
        index = [b[0] for b in BEATS].index(name)
        y = self.h - 18*self.s
        d.line((self.w*.12, y, self.w*.88, y), fill="#F1F5F9", width=1)
        for i in range(len(BEATS)):
            x = self.w/2 + (i-2.5)*12*self.s
            radius = (2 if i == index else 1)*self.s
            d.ellipse((x-radius, y-radius, x+radius, y+radius), fill=theme.ACCENT if i <= index else theme.BORDER)


class DecisionTheatre:
    """One after driver, no threads/global bindings. A hidden canvas never animates.

    Frames are scheduled toward fixed deadlines on a monotonic clock; each frame shows
    the content of the latest deadline, so timer granularity never alters motion.
    """

    def __init__(self, parent):
        import tkinter as tk
        self.canvas = tk.Canvas(parent,bg=theme.CARD,highlightthickness=0,bd=0,
                                width=theme.px(300),height=theme.px(320),takefocus=True)
        self.scene = self.renderer = self.photo = None
        self.after_id = None
        self.pending = False
        self.running = False
        self.dead = False
        self.elapsed = DURATION
        self.timer = TimerResolution()
        self.log = None
        self.item = self.canvas.create_image(0,0,anchor="nw")
        self.top = self.canvas.winfo_toplevel()
        self.escape_binding = self.top.bind("<Escape>",self.escape,add="+")
        self.canvas.bind("<Configure>",self.configure)
        self.canvas.bind("<Map>",self.mapped)
        self.canvas.bind("<Unmap>",lambda e:self.stop())
        self.canvas.bind("<Destroy>",self.destroy)
        self.canvas.bind("<Button-1>",lambda e:self.skip())
        for key in ("<space>","<Return>","<Escape>"):
            self.canvas.bind(key,lambda e:self.skip())

    def cancel(self):
        if self.after_id is not None:
            self.canvas.after_cancel(self.after_id)
            self.after_id = None

    def stop(self):
        self.cancel()
        self.timer.end()
        self.pending = self.running = False
        self.elapsed = DURATION

    def destroy(self,event):
        if event.widget is self.canvas:
            self.stop()
            self.top.unbind("<Escape>",self.escape_binding)
            self.dead = True
            self.photo = self.renderer = None

    def escape(self,event):
        if self.running:
            return self.skip()

    def play(self,result,animate=True):
        self.stop()
        self.scene = scene_from_result(result)
        self.renderer = None
        self.pending = bool(animate)
        if self.canvas.winfo_ismapped():
            self.start()

    def start(self):
        if self.dead or not self.scene:
            return
        self.cancel()
        self.running,self.pending = self.pending,False
        self.elapsed = 0. if self.running else DURATION
        self.shown = -1
        if self.running:
            self.timer.begin()
        self.started = time.perf_counter()
        self.tick()

    def mapped(self,event):
        self.start()

    def configure(self,event):
        self.renderer = None
        if self.running:
            # Resize settles the film and cancels its old geometry's clock.
            self.skip()
        elif self.scene:
            self.draw()

    def draw(self):
        if self.dead or self.scene is None:
            return
        if self.renderer is None:
            self.renderer = TheatreRenderer(self.scene,max(1,self.canvas.winfo_width()),
                                             max(1,self.canvas.winfo_height()),theme.FACTOR)
        frame = self.renderer.frame(self.elapsed)
        if self.photo is None or self.photo.width()!=frame.width or self.photo.height()!=frame.height:
            self.photo = ImageTk.PhotoImage(frame,master=self.canvas)
            self.canvas.itemconfigure(self.item,image=self.photo)
        else:
            self.photo.paste(frame)

    def tick(self):
        self.after_id = None
        if self.dead or not self.canvas.winfo_ismapped():
            self.stop()
            return
        index = None
        if self.running:
            index = round(frame_time(time.perf_counter()-self.started) / FRAME)
            self.elapsed = min(DURATION, index*FRAME)
        if index is None or index != self.shown:
            self.draw()
            self.shown = index
            if self.log is not None:
                self.log.append((time.perf_counter(), self.elapsed))
        if self.running and self.elapsed < DURATION:
            self.after_id = self.canvas.after(next_delay_ms(self.started, time.perf_counter(), index+1), self.tick)
        else:
            self.running = False
            self.timer.end()

    def skip(self):
        self.stop()
        self.draw()
        return "break"
