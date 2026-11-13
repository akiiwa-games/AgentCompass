"""The decision compass page: radar, verdict, ledger replay and the decision theatre.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr, tr

import radar_chart
import decision_theatre
import decision_summary
import quota_line
import display_names
import ui_theme as theme
from panel_widgets import clip, fmt, freeze_redraw
import panel_widgets


AUTO_REPLAY_ROWS = 5  # newest ledger rows tried for the opening decision; legacy rows replan, so keep it small


def verdict_summary(result: dict, pair: list[dict]) -> tuple[list[tuple[str, str]], str]:
    """(label, value) rows for the compact verdict grid under the radar, and the full router reason for its tooltip.

    Values come from the plan's structured fields; the long reason only fills the quota row when no capacity is recorded.
    """
    decision = (result or {}).get("decision") or {}
    reason = " ".join(str(decision.get("reason") or "").split())
    if not pair:
        if (result or {}).get("status") == "blocked" or (result or {}).get("error") == "scorer_unavailable":
            scorer = (result or {}).get("scorer") or {}
            detail = scorer.get("fallback_detail") or scorer.get("fallback_reason") or (result or {}).get("error") or "scorer_unavailable"
            return [("", _tr("未完成评分：小模型分类器不可用（{0}）").format(detail))], ""
        return [("", reason or _tr("没有可用候选"))], ""
    first = pair[0]
    rows = [(_tr("胜出"), f"{decision_summary.candidate_label(first)}  {fmt(first['score'])}")]
    if len(pair) > 1:
        second = pair[1]
        lead = (first.get("score") or 0.0) - (second.get("score") or 0.0)
        gap = _tr("并列") if abs(lead) < 0.00005 else _tr("领先 {0}").format(fmt(lead, 4))
        rows.append((_tr("次选"), f"{decision_summary.candidate_label(second)}  {fmt(second['score'])} · {gap}"))
    rows.append((_tr("任务"), decision_summary.task_label(result)))
    candidate = next((c for c in (result or {}).get("ranking") or [] if c.get("id") == first["id"]), {})
    capacity = candidate.get("capacity") if isinstance(candidate.get("capacity"), dict) else {}
    pct = candidate.get("expected_pct", capacity.get("step_pct"))
    cost = (first.get("components") or {}).get("opportunity_cost", capacity.get("opportunity_cost"))
    if capacity.get("window"):
        parts = [_tr("{0} 剩 {1}%").format(capacity["window"], fmt(capacity.get("remaining"), 0))]
        if isinstance(pct, (int, float)):
            parts.append(_tr("本单 ~{0}%").format(fmt(pct, 1)))
        if isinstance(cost, (int, float)):
            parts.append(_tr("机会成本 {0}").format(fmt(cost, 3)))
        rows.append((_tr("额度"), " · ".join(parts)))
    elif reason:
        rows.append((_tr("额度"), clip(reason.split(";", 1)[0], 72)))
    return rows, reason


def pair_ids(pair: list[dict]) -> str:
    """Raw candidate ids of the verdict pair, kept for the tooltip and the plain-text mirror."""
    return "ID  " + " · ".join(str(r.get("id")) for r in pair) if pair else ""


def bundle_labels(bundles: list[dict]) -> list[str]:
    """Display names for an account's bundles; a name shared by two bundles falls back to the bundle id."""
    counts = {}
    for b in bundles:
        counts[b["label"]] = counts.get(b["label"], 0) + 1
    return [b["label"] if counts[b["label"]] == 1 else b["id"] for b in bundles]


class ScoringPage:
    # ---------- page 1: decision compass ----------
    def build_scoring(self, page):
        tk, ttk = self.tk, self.ttk
        self.header(page, _tr("决策罗盘"), ((_tr("刷新"), self.load_ledger, "SmallAccent.TButton"),))
        # One chart: decision components when available, otherwise the model catalog.
        self.mode = tk.StringVar(value="score")
        body = ttk.Frame(page)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, uniform="split")
        body.columnconfigure(1, weight=1, uniform="split")
        body.rowconfigure(0, weight=1)

        radar_card = theme.card(body, padding=12)
        radar_card.outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.radar_title = ttk.Label(radar_card, text=radar_chart.axis_title(), style="CardH3.TLabel")
        self.radar_title.pack(anchor="w")
        self.radar_source = ttk.Label(radar_card, style="CardTiny.TLabel")
        self.radar_source.pack(anchor="w", pady=(2, 8))
        theatre_controls = ttk.Frame(radar_card, style="Card.TFrame")
        theatre_controls.pack(fill="x", pady=(0, 4))
        self.theatre_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(theatre_controls, text=_tr("动画"), variable=self.theatre_enabled,
                        command=self.toggle_theatre).pack(side="right")
        replay = ttk.Button(theatre_controls, text="↻", style="Tiny.TButton", width=3,
                            command=self.replay_theatre)
        replay.pack(side="right", padx=(0, 5))
        replay._tooltip_text = _tr("重播 · 点击画面或按 Esc 跳过")
        self.attach_tooltip(replay)
        picks = ttk.Frame(radar_card, style="Card.TFrame")
        self.radar_picks = picks
        picks.pack(fill="x")
        tk.Label(picks, text="●", bg=theme.CARD, fg=theme.ACCENT, font=(theme.FONT, 10)).pack(side="left")
        self.pick_a = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_a.pack(side="left", padx=(3, 10))
        tk.Label(picks, text="◌", bg=theme.CARD, fg=theme.CYAN, font=(theme.FONT, 10, "bold")).pack(side="left")
        self.pick_b = ttk.Combobox(picks, state="readonly", width=17)
        self.pick_b.pack(side="left", padx=(3, 0))
        self.pick_a.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.pick_b.bind("<<ComboboxSelected>>", lambda e: self.render_radar())
        self.verdict = tk.StringVar(value="")  # plain-text mirror of the verdict grid
        # Label/value rows instead of one dense paragraph; the full router reason is the grid's hover tooltip.
        self.verdict_box = ttk.Frame(radar_card, style="Card.TFrame")
        self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
        self.verdict_box.columnconfigure(1, weight=1)
        self.verdict_box.bind("<Configure>", lambda e: self.wrap_verdict(e.width))
        self._verdict_width = None
        self.radar = radar_chart.RadarChartCanvas(radar_card, size=theme.px(300), bg=theme.CARD)
        self.radar.pack(fill="both", expand=True, pady=(6, 0))
        self.theatre = decision_theatre.DecisionTheatre(radar_card)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        sim = theme.card(right, padding=12, fill="x")
        self.preset = ttk.Combobox(sim, state="readonly", values=[p[0] for p in self.data_presets()])
        self.preset.set(_tr("典型场景…"))
        self.preset.pack(fill="x")
        self.preset.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        self.objective = tk.Text(sim, height=3, width=30, wrap="word", font=(theme.FONT, 9), relief="flat", bd=0,
                                 bg="#F8FAFC", fg=theme.INK, highlightthickness=1, highlightbackground=theme.BORDER,
                                 highlightcolor=theme.ACCENT, padx=6, pady=4)
        self.objective.pack(fill="x", pady=8)
        options = ttk.Frame(sim, style="Card.TFrame")
        options.pack(fill="x")
        # The button is packed first so it keeps its space at any width; the options flow onto
        # extra rows when the window is too narrow for one line (the minimum size, English).
        self.simulate_button = ttk.Button(options, text=_tr("模拟评分"), style="SmallAccent.TButton", command=self.simulate)
        self.simulate_button.pack(side="right", anchor="n", padx=(8, 0))
        flow = ttk.Frame(options, style="Card.TFrame")
        flow.pack(side="left", fill="x", expand=True)
        self.hours = tk.StringVar(value="2")
        hours_box = ttk.Frame(flow, style="Card.TFrame")
        ttk.Spinbox(hours_box, from_=0.5, to=24, increment=0.5, textvariable=self.hours, width=4).pack(side="left")
        ttk.Label(hours_box, text="h", style="Card.TLabel").pack(side="left", padx=(3, 2))
        self.local_only = tk.BooleanVar()
        self.needs_visual = tk.BooleanVar()
        self.live_scorer = tk.BooleanVar(value=True)
        self._option_items = [hours_box] + [ttk.Checkbutton(flow, text=_tr(text), variable=variable) for text, variable in
                                            (("仅本机", self.local_only), ("需视觉", self.needs_visual))]
        self._option_rows, self._option_layout = [], None
        flow.bind("<Configure>", lambda event: self.reflow_options(flow, event.width))
        self.reflow_options(flow, 10 ** 6)

        lists = theme.card(right, padding=12, fill="both", expand=True, pady=(12, 0))
        top = ttk.Frame(lists, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 8))
        self.list_switch = theme.Segmented(top, (("ledger", _tr("最近决策")), ("breakdown", _tr("候选明细")),
                                                 ("quota", _tr("额度恢复线"))), self.show_list)
        self.list_switch.frame.pack(side="left")
        stack = ttk.Frame(lists, style="Card.TFrame")
        self.list_stack = stack
        stack.pack_propagate(False)
        stack.pack(fill="both", expand=True)
        self.ledger_frame, self.ledger_table = self.tree(stack, (
            ("at", _tr("时间"), 72, "w"), ("kind", _tr("类型"), 108, "w"), ("choice", _tr("胜出"), 132, "w"), ("task", _tr("任务"), 120, "w")), 8)
        self.ledger_table.bind("<<TreeviewSelect>>", lambda e: self.replay_selected())
        self.attach_row_tooltip(self.ledger_table, self.ledger_row_tip)
        self.breakdown_frame, self.breakdown = self.tree(stack, (
            ("id", _tr("候选"), 108, "w"), ("score", _tr("总分"), 48, "e"), ("fit", _tr("契合"), 50, "e"),
            ("opp", _tr("额度成本"), 62, "e"), ("boost", _tr("加成"), 50, "e"), ("note", _tr("状态"), 44, "w")), 8)
        self.breakdown.bind("<<TreeviewSelect>>", lambda e: self.pick_from_breakdown())
        self.attach_row_tooltip(self.breakdown, self.breakdown_row_tip)
        # The quota-line view of the same decision; it fills in on the film's clock while the film plays.
        self.quota_frame = ttk.Frame(stack, style="Card.TFrame")
        self.quota_line = quota_line.QuotaLineCanvas(self.quota_frame)
        self.quota_line.canvas.pack(fill="both", expand=True)
        self.attach_quota_tooltip(self.quota_line, _tr(
            "每个小图是一个额度窗口：从左到右是本周期（右端是重置），从下到上是额度。浅色直线是额度线：按订阅的节奏应该用到哪里。"
            "粗线是实际用量：在额度线下面是绿色，这个窗口被优先使用；超过额度线变红，这个窗口被降权。"
            "点线是在重置前把剩余额度用完所需的节奏。高亮的窗口是本次任务用的。点击画面重播。"))
        self.theatre.on_frame = self.quota_line.follow
        self.show_list("ledger")
        self.set_mode("score")
        self.prefill(lambda: self.data_module().model_table(), self.models_loaded)
        self.prefill(lambda: self.data_module().recent_ledger_with_skipped(30), self.ledger_loaded)
        if self.plan_result:
            self.plan_loaded(self.plan_result)

    def show_list(self, which):
        self.list_switch.set(which)
        with freeze_redraw(getattr(self, "list_stack", None)):
            for name, frame in (("ledger", self.ledger_frame), ("breakdown", self.breakdown_frame),
                                ("quota", self.quota_frame)):
                if name == which:
                    frame.pack(fill="both", expand=True)
                else:
                    frame.pack_forget()

    def data_presets(self):
        from router_data import PRESETS

        return PRESETS

    def apply_preset(self):
        name = self.preset.get()
        for label, objective, hours, local, visual in self.data_presets():
            if label == name:
                self.objective.delete("1.0", "end")
                self.objective.insert("1.0", objective)
                self.hours.set(f"{hours:g}")
                self.local_only.set(local)
                self.needs_visual.set(visual)

    def set_mode(self, mode):
        self.mode.set(mode)
        if mode == "dna":
            if not self.models:
                try:
                    self.models = self.data_module().model_table()
                except Exception as exc:
                    panel_widgets.log_error(exc)
        self.fill_pickers()
        self.render_radar()

    def models_loaded(self, models):
        self.models = models or {}
        if "scoring" in self.built and not self.plan_result:
            self.set_mode("dna")

    def fill_pickers(self):
        self.pick_ids = {}
        if self.mode.get() == "dna":
            for m in self.models:
                label = self.data_module().model_label(m)
                self.pick_ids[m if label in self.pick_ids else label] = m
            labels = list(self.pick_ids)
            defaults = list(self.pick_ids.values())[:2]
        else:
            rows = [r for r in self.data_module().candidate_rows(self.plan_result or {}) if not r["excluded"]]
            labels = display_names.unique_labels(rows, self.data_module().account_label)
            self.pick_ids = dict(zip(labels, (r["id"] for r in rows)))
            pair = self.data_module().decision_pair(self.plan_result or {})
            defaults = [r["id"] for r in pair]
        self.pick_a.configure(values=labels)
        self.pick_b.configure(values=[_tr("（自动：次选模型）")] + labels if self.mode.get() == "score" else labels)
        for picker, wanted in ((self.pick_a, defaults[0] if defaults else None),
                               (self.pick_b, defaults[1] if len(defaults) > 1 else None)):
            match = next((label for label in labels if wanted and self.pick_ids.get(label, label) == wanted), "")
            picker.set(match)
        if self.mode.get() == "score" and not self.pick_b.get():
            self.pick_b.set(_tr("（自动：次选模型）"))

    def picked(self, picker):
        value = picker.get()
        if not value or value == _tr("（自动：次选模型）"):
            return None
        return getattr(self, "pick_ids", {}).get(value, value)

    def render_radar(self):
        if hasattr(self, "theatre"):
            self.theatre.stop()
            self.theatre.canvas.pack_forget()
            self.radar_title.pack(anchor="w")
            self.radar_source.pack(anchor="w", pady=(2, 8))
            self.radar_picks.pack(fill="x")
            self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
            self.radar.pack(fill="both", expand=True, pady=(6, 0))
        data = self.data_module()
        self.radar_title.configure(text=radar_chart.axis_title(self.mode.get()))
        self.radar_source.configure(text=_tr("模型能力 · 静态价格经济性") if self.mode.get() == "dna"
                                    else _tr("决策候选 · 本次额度成本"))
        if self.mode.get() == "dna":
            if not self.models:
                self.radar.show((), [])
                return
            series, raw = data.dna_series(self.models, self.picked(self.pick_a), self.picked(self.pick_b))
            self.radar.show(radar_chart.DNA_AXES, series, raw)
            self.show_verdict([("", " vs ".join(s["label"] for s in series))])
            return
        if not self.plan_result:
            self.radar.show((), [])
            self.show_verdict([])
            return
        pair = data.decision_pair(self.plan_result, self.picked(self.pick_a), self.picked(self.pick_b))
        series, raw = data.decision_series(pair)
        self.radar.show(radar_chart.SCORE_AXES, series, raw)
        self.show_verdict(*verdict_summary(self.plan_result, pair))

    def verdict_text(self):
        """The verdict as plain text (label: value per line), as the grid shows it for the router's own pair."""
        pair = self.data_module().decision_pair(self.plan_result or {})
        rows, _ = verdict_summary(self.plan_result or {}, pair)
        lines = [f"{label}  {value}" if label else value for label, value in rows]
        return "\n".join(lines + ([pair_ids(pair)] if pair else []))

    def show_verdict(self, rows, reason: str = ""):
        box = self.verdict_box
        for child in box.winfo_children():
            child.destroy()
        self._verdict_values, self._verdict_keys = [], []
        self.verdict.set("\n".join(f"{label}  {value}" if label else value for label, value in rows))
        if self.mode.get() == "score" and self.plan_result:
            pair = self.data_module().decision_pair(self.plan_result, self.picked(self.pick_a), self.picked(self.pick_b))
            if pair:
                self.verdict.set(self.verdict.get() + "\n" + pair_ids(pair))
                self.verdict_strip = decision_summary.SummaryCanvas(box, decision_summary.summary_model(self.plan_result,pair))
                canvas = self.verdict_strip.canvas
                canvas.grid(row=0,column=0,columnspan=2,sticky="ew")
                box.columnconfigure(0,weight=1)
                box.columnconfigure(1,weight=0)
                canvas._tooltip_text = self.verdict.get() + ("\n"+reason if reason else "")
                self.attach_tooltip(canvas)
                return
        box.columnconfigure(0,weight=0)
        box.columnconfigure(1,weight=1)
        for index, (label, value) in enumerate(rows):
            widgets = []
            if label:
                hint = label + (" ⓘ" if reason and index == len(rows) - 1 else "")
                key = self.ttk.Label(box, text=hint, style="CardSub.TLabel")
                key.grid(row=index, column=0, sticky="nw", padx=(0, theme.px(10)), pady=1)
                widgets.append(key)
                self._verdict_keys.append(key)
            text = self.ttk.Label(box, text=value, style="Card.TLabel", font=(theme.FONT, 9), foreground=theme.INK,
                                  justify="left", anchor="w")
            text.grid(row=index, column=1 if label else 0, columnspan=1 if label else 2, sticky="w", pady=1)
            widgets.append(text)
            self._verdict_values.append((text, bool(label)))
            if reason:
                for widget in widgets:
                    widget._tooltip_text = reason
                    self.attach_tooltip(widget)
        self.verdict.set("\n".join(f"{label}  {value}" if label else value for label, value in rows))
        self._verdict_width = None
        self.wrap_verdict(box.winfo_width())

    def wrap_verdict(self, width):
        """Wrap each value to the grid's real width so nothing is clipped at any window size or language."""
        if width <= 1 or width == self._verdict_width:
            return
        self._verdict_width = width
        key_width = max((w.winfo_reqwidth() for w in getattr(self, "_verdict_keys", [])), default=0) + theme.px(10)
        for label, keyed in getattr(self, "_verdict_values", []):
            label.configure(wraplength=max(theme.px(120), width - (key_width if keyed else 0) - theme.px(4)))

    def reflow_options(self, flow, width):
        """Lay the simulation options out left to right, starting a new row when the next one would not fit."""
        if width <= 1:
            return
        gap, layout, row, used = 8, [], 0, 0
        for widget in self._option_items:
            need = widget.winfo_reqwidth()
            if used and used + gap + need > width:
                row, used = row + 1, 0
            layout.append(row)
            used += (gap if used else 0) + need
        if layout == self._option_layout:
            return
        self._option_layout = layout
        while len(self._option_rows) <= row:
            frame = self.ttk.Frame(flow, style="Card.TFrame")
            frame.pack(fill="x", pady=(4 if self._option_rows else 0, 0))
            self._option_rows.append(frame)
        for frame in self._option_rows[row + 1:]:
            frame.pack_forget()
        for index, frame in enumerate(self._option_rows[:row + 1]):
            frame.pack(fill="x", pady=(4 if index else 0, 0))
        for index, widget in enumerate(self._option_items):
            first = index == 0 or layout[index] != layout[index - 1]
            widget.pack_forget()
            widget.pack(in_=self._option_rows[layout[index]], side="left", padx=(0 if first else gap, 0))
            widget.lift()  # the row frames are created later, so they would otherwise stack above the widgets

    def simulate(self):
        objective = self.objective.get("1.0", "end").strip()
        if not objective:
            self.status.set(_tr("先输入任务简述或选择一个典型场景"))
            return
        try:
            hours = float(self.hours.get())
            if hours <= 0:
                raise ValueError
        except ValueError:
            self.status.set(_tr("预计工时需为正数"))
            return
        self.simulate_button.configure(state="disabled", text=_tr("评分中…"))
        local, visual, live = self.local_only.get(), self.needs_visual.get(), self.live_scorer.get()

        def work():
            try:
                return self.data_module().simulate(objective, hours, local, visual, live)
            except Exception as exc:
                panel_widgets.log_error(exc)
                return exc

        def done(result):
            if isinstance(result, Exception):
                self.simulate_button.configure(state="normal", text=_tr("模拟评分"))
                self.status.set(_tr('模拟评分失败：{0}').format(f'{result}'))
                return
            self.plan_loaded(result)
            self.show_list("breakdown")
        self.run(work, done, _tr("正在用小模型打分并模拟路由…") if live else _tr("正在模拟路由评分…"))

    def replay_selected(self):
        if getattr(self, "_suppress_next_select", False):
            self._suppress_next_select = False
            return
        selected = self.ledger_table.selection()
        if not selected:
            return
        row = self.ledger_rows[int(selected[0])]
        self.run(lambda: self.data_module().replay(row), self.plan_loaded,
                 tr("正在读取台账中保存的该次决策…", "Loading this decision from the ledger…"))

    def plan_loaded(self, result):
        self.simulate_button.configure(state="normal", text=_tr("模拟评分"))
        self.plan_result = result or {}
        # The quota view keeps a record it cannot read to itself: the rest of the decision always loads.
        self.quota_line.show(self.plan_result)
        if self.quota_line.error is not None:
            panel_widgets.log_error(self.quota_line.error)
        data = self.data_module()
        rows = data.candidate_rows(self.plan_result)
        if self.plan_result.get("theatre_source") == "unreplayable":
            self.breakdown_rows = rows
            self.breakdown.delete(*self.breakdown.get_children())
            self.status.set(self.plan_result["replay_note"])
            return
        self.breakdown_rows = rows
        self.breakdown.delete(*self.breakdown.get_children())
        self.breakdown_labels = display_names.unique_labels(rows, data.account_label)
        for index, row in enumerate(rows):
            excluded = row["excluded"]
            winner = row["id"] == self.plan_result.get("choice")
            note = _tr("拦截") if excluded else _tr("胜出") if winner else _tr("可选")
            self.breakdown.insert("", "end", iid=str(index), tags=("muted",) if excluded else ("win",) if winner else (),
                                  values=(self.breakdown_labels[index], fmt(row["score"]), fmt(row["fit"], 2),
                                          fmt(row["opportunity_cost"], 3), fmt(row["boost"], 2, True),
                                          note))
        if self.mode.get() != "score":
            self.set_mode("score")
        else:
            self.fill_pickers()
            self.render_radar()
        choice = self.plan_result.get("choice")
        winner = next((label for label, row in zip(self.breakdown_labels, rows) if row["id"] == choice), choice or "—")
        scorer = self.plan_result.get("scorer") or {}
        if self.plan_result.get("status") == "blocked" or self.plan_result.get("error") == "scorer_unavailable":
            detail = scorer.get("fallback_detail") or scorer.get("fallback_reason") or self.plan_result.get("error") or "scorer_unavailable"
            message = _tr('未完成评分：小模型分类器不可用（{0}）').format(f"{detail}")
        else:
            message = _tr('评分完成：{0} 个候选，胜出 {1}').format(f'{len(rows)}', f"{winner}")
            if self.plan_result.get("theatre_source") == "recomputed":
                message += tr("（按当前额度重算）", " (recomputed with current quota)")
            if scorer:
                source = scorer.get("source")
                message += (_tr('（小模型 {0} · {1}ms）').format(f"{scorer.get('model')}", f"{scorer.get('duration_ms')}") if source == "system_one"
                            else _tr('（小模型不可用：{0}，请主会话自主选择）').format(f"{scorer.get('fallback_detail') or scorer.get('fallback_reason')}"))
        self.status.set(message)
        self.replay_theatre()

    def replay_theatre(self):
        if not self.plan_result or not hasattr(self, "theatre"):
            return
        self.radar.canvas.pack_forget()
        self.radar_title.pack_forget()
        self.radar_source.pack_forget()
        self.radar_picks.pack_forget()
        self.verdict_box.pack(side="bottom", fill="x", pady=(4, 0))
        self.theatre.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.theatre.canvas.update_idletasks()
        self.theatre.play(self.plan_result, self.theatre_enabled.get())

    def toggle_theatre(self):
        if not self.theatre_enabled.get():
            self.theatre.skip()

    def pick_from_breakdown(self):
        selected = self.breakdown.selection()
        if not selected or not selected[0].isdigit():
            return
        index = int(selected[0])
        row = self.breakdown_rows[index]
        if row["excluded"]:
            self.status.set(_tr('{0} 被拦截：{1}').format(f"{self.breakdown_labels[index]}", f"{'；'.join(row['excluded'])}"))
            return
        # Clicking an eligible row makes it the comparison series against the primary.
        label = next((label for label, bundle in getattr(self, "pick_ids", {}).items() if bundle == row["id"]), None)
        if self.mode.get() == "score" and label in self.pick_a.cget("values") and row["id"] != self.picked(self.pick_a):
            self.pick_b.set(label)
            self.render_radar()

    def load_ledger(self):
        self.run(lambda: self.data_module().recent_ledger_with_skipped(30), self.ledger_loaded)

    def ledger_loaded(self, payload):
        if isinstance(payload, tuple) and len(payload) == 2:
            rows, skipped = payload
        else:
            rows, skipped = payload, 0
        self.ledger_rows = rows or []
        if skipped:
            self.status.set(_tr("已跳过 {0} 条旧格式台账记录").format(skipped))
        self.ledger_table.delete(*self.ledger_table.get_children())
        for index, row in enumerate(self.ledger_rows):
            s = self.data_module().ledger_summary(row)
            self.ledger_table.insert("", "end", iid=str(index),
                                     values=(s["at"], s["kind_label"], s["choice_label"], s["objective"]))
        # Open on the latest real decision so the radar is never empty on first sight.
        if self.ledger_rows and not self.plan_result and not self.auto_replayed:
            self.auto_replayed = True
            self.prefill(self.latest_decision, self.auto_selected)

    def latest_decision(self):
        """The newest ledger row whose replay names a winner; a None result has no film to play."""
        for index, row in enumerate(self.ledger_rows[:AUTO_REPLAY_ROWS]):
            try:
                result = self.data_module().replay(row)
            except Exception as exc:
                panel_widgets.log_error(exc)
                continue
            if result and result.get("choice"):
                return index, result
        return None

    def auto_selected(self, found):
        if found is None:
            return
        index, result = found
        self._suppress_next_select = True
        self.ledger_table.selection_set(str(index))
        self.ledger_table.see(str(index))
        self.plan_loaded(result)

    def ledger_row_tip(self, iid):
        rows = getattr(self, "ledger_rows", ())
        if not iid.isdigit() or int(iid) >= len(rows):
            return ""
        row = rows[int(iid)]
        return f"{row.get('kind') or '—'} · {row.get('choice') or '—'}"

    def breakdown_row_tip(self, iid):
        rows = getattr(self, "breakdown_rows", ())
        if not iid.isdigit() or int(iid) >= len(rows):
            return ""
        row = rows[int(iid)]
        return " · ".join(str(v) for v in (row["id"], row.get("account")) if v)

    def attach_quota_tooltip(self, view, legend):
        """Hover text for the quota-line view: the mark under the pointer in words, else how to read the picture."""
        canvas, current = view.canvas, {"text": None}
        show, hide = self.attach_tooltip(canvas, bind=False)

        def move(event):
            text = view.tip_at(event.x, event.y) or legend
            if text == current["text"]:
                return
            hide()
            current["text"] = canvas._tooltip_text = text
            show(event)

        def leave(_event=None):
            current["text"] = None
            hide()

        def replay(_event=None):
            leave()
            self.replay_theatre()
        canvas.bind("<Motion>", move, add="+")
        canvas.bind("<Leave>", leave, add="+")
        canvas.bind("<Button-1>", replay, add="+")
