"""Compact static verdict: the pair by name and the task; exact values live in the tooltip."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageTk
import ui_theme as theme
from decision_theatre import TheatreRenderer, Scene, finite
from display_names import WORK_KINDS, DIFFICULTIES, EFFORTS, kind_label, difficulty_label, candidate_label  # noqa: F401


def task_label(result):
    task = (result.get("decision") or {}).get("task") or result.get("task") or {}
    hours = task.get("hours",task.get("expected_hours"))
    return " · ".join((kind_label(task.get("kind",task.get("work_kind"))),
                       difficulty_label(task.get("difficulty")),f"{hours:g}h" if finite(hours) else "—"))


def summary_model(result,pair):
    return dict(names=[candidate_label(r) for r in pair[:2]],task=task_label(result))


class SummaryRenderer:
    def __init__(self,model,width,scale=1.):
        self.model=model
        self.w=max(1,int(width)); self.s=min(scale,self.w/280)
        self.height=round(82*self.s)
        self.painter=TheatreRenderer(Scene("","",(),(),None,False),self.w,max(300,self.height),self.s)
        # Typography alone is reused; no clock, animation or connection to the theatre.
        self.painter.s=self.s

    def render(self):
        image=Image.new("RGB",(self.w,max(1,self.height)),theme.CARD)
        d=ImageDraw.Draw(image)
        text=self.painter.text
        s=self.s
        for i,name in enumerate(self.model['names']):
            y=(16+24*i)*s; color=theme.ACCENT if i==0 else theme.CYAN
            r=(3.4 if i==0 else 2.8)*s; x=6*s
            d.ellipse((x-r,y-r,x+r,y+r),fill=color)
            text(image,name,(16*s,y),11,color,i==0,"lm",self.w-22*s)
        text(image,self.model['task'],(3*s,68*s),10,theme.MUTED,anchor="lm",max_width=self.w-6*s)
        return image


class SummaryCanvas:
    def __init__(self,parent,model):
        import tkinter as tk
        self.model=model
        self.canvas=tk.Canvas(parent,bg=theme.CARD,highlightthickness=0,bd=0,height=theme.px(82))
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
