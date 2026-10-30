"""Compact static verdict: colour/position carry rank, exact values live in the tooltip."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageTk
from i18n import translate as _tr
import radar_chart
import ui_theme as theme
from decision_theatre import TheatreRenderer, Scene, finite
from display_names import WORK_KINDS, DIFFICULTIES, EFFORTS, kind_label, difficulty_label, candidate_label  # noqa: F401


def task_label(result):
    task = (result.get("decision") or {}).get("task") or result.get("task") or {}
    hours = task.get("hours",task.get("expected_hours"))
    return " · ".join((kind_label(task.get("kind",task.get("work_kind"))),
                       difficulty_label(task.get("difficulty")),f"{hours:g}h" if finite(hours) else "—"))


def score_segments(scores,floor=None):
    """Shared linear scale including zero; negative scores extend left of the origin."""
    values=[s for s in scores if finite(s)]
    low=min([float(floor),*values]) if finite(floor) else min([0.,*values])
    high=max([low,*values])
    span=high-low or 1.
    origin=0. if finite(floor) else -low/span
    return origin, [((s-low)/span if finite(s) else origin) for s in scores]


def quota_segments(remaining,expected):
    if not finite(remaining):
        return None
    end=radar_chart.clamp(remaining/100)
    slice_=radar_chart.clamp(expected/100) if finite(expected) else 0.
    return max(0.,end-slice_),end


def summary_model(result,pair):
    first=pair[0] if pair else {}
    candidate=next((c for c in result.get("ranking") or [] if c.get("id")==first.get("id")),{})
    capacity=candidate.get("capacity") or {}
    quota=quota_segments(capacity.get("remaining"),candidate.get("expected_pct",capacity.get("step_pct")))
    scores=[c['score'] for c in result.get('ranking') or [] if not c.get('excluded') and finite(c.get('score'))]
    return dict(names=[candidate_label(r) for r in pair[:2]],scores=[r.get("score") for r in pair[:2]],
                floor=min(scores) if scores else None,
                task=task_label(result),quota=quota,fallback=_tr("额度未记录") if quota is None else "")


class SummaryRenderer:
    def __init__(self,model,width,scale=1.):
        self.model=model
        self.w=max(1,int(width)); self.s=min(scale,self.w/280)
        self.height=round(108*self.s)
        self.painter=TheatreRenderer(Scene("","",(),(),None,False),self.w,max(300,self.height),self.s)
        # Typography alone is reused; no clock, animation or connection to the theatre.
        self.painter.s=self.s

    def render(self):
        image=Image.new("RGB",(self.w,max(1,self.height)),theme.CARD)
        d=ImageDraw.Draw(image)
        text=self.painter.text
        s=self.s
        origin,ends=score_segments(self.model['scores'],self.model.get('floor'))
        left,right=self.w*.61,self.w-8*s
        for i,(name,end) in enumerate(zip(self.model['names'],ends)):
            y=(18+28*i)*s; color=theme.ACCENT if i==0 else theme.CYAN
            r=3*s; x=5*s
            d.ellipse((x-r,y-r,x+r,y+r),fill=color if i==0 else theme.CARD,outline=color,width=max(1,round(s)))
            text(image,name,(15*s,y),11,color,i==0,"lm",self.w*.54)
            d.line((left,y,right,y),fill=theme.TRACK,width=max(1,round(2*s)))
            a=left+(right-left)*origin; b=left+(right-left)*end
            if abs(a-b)>.1:
                d.line((a,y,b,y),fill=color,width=max(1,round(2*s)))
            if i==0 and len(ends)>1 and end>ends[1]:
                lead_start=left+(right-left)*ends[1]
                d.line((lead_start,y,b,y),fill="#312E81",width=max(2,round(3*s)))
                d.line((b,y-3*s,b,y+3*s),fill=theme.ACCENT,width=max(1,round(s)))
        text(image,self.model['task'],(3*s,75*s),10,theme.MUTED,anchor="lm",max_width=self.w-6*s)
        q=self.model['quota']
        if q is not None:
            a,b=q; x0,x1=3*s,self.w-8*s; y=97*s
            d.line((x0,y,x1,y),fill=theme.TRACK,width=max(1,round(4*s)))
            if a>0:
                d.line((x0,y,x0+(x1-x0)*a,y),fill="#C7D2FE",width=max(1,round(4*s)))
            if b>a:
                d.line((x0+(x1-x0)*a,y,x0+(x1-x0)*b,y),fill=theme.ACCENT,width=max(1,round(4*s)))
        else:
            text(image,self.model['fallback'],(3*s,97*s),9,theme.FAINT,anchor="lm")
        return image


class SummaryCanvas:
    def __init__(self,parent,model):
        import tkinter as tk
        self.model=model
        self.canvas=tk.Canvas(parent,bg=theme.CARD,highlightthickness=0,bd=0,height=theme.px(108))
        self.photo=None
        self.item=self.canvas.create_image(0,0,anchor="nw")
        self.last_width=None
        self.canvas.bind("<Configure>",self.configure)

    def configure(self,event):
        if event.width<=1 or event.width==self.last_width:
            return
        self.last_width=event.width
        renderer=SummaryRenderer(self.model,event.width,theme.FACTOR)
        self.canvas.configure(height=renderer.height)
        self.photo=ImageTk.PhotoImage(renderer.render(),master=self.canvas)
        self.canvas.itemconfigure(self.item,image=self.photo)
