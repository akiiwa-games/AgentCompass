"""Model settings tab "strategy" (评分与能力): the user's settings, the four categories with the quota economy, and the
models' category scores with local manual scores.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr

import ui_theme as theme
from panel_widgets import ScrollFrame, TITLE


STRATEGY_PAD = 14    # card padding on the strategy settings page


STRATEGY_FIELD = 10  # entry and spinbox text size there, matching the 10pt labels beside them


def matching_preset(tuning: dict, presets: dict) -> str | None:
    """The strategy preset the saved tuning equals, if any."""
    for name, preset in presets.items():
        try:
            if all(abs(float(tuning.get(k)) - float(v)) < 1e-6 for k, v in preset.items()):
                return name
        except (TypeError, ValueError):
            continue
    return None


class StrategyTab:
    def build_setting_strategy(self, frame):
        footer = self.ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.strategy_save = self.ttk.Button(footer, text=_tr("保存额度阈值"), style="SmallAccent.TButton", command=self.save_tuning)
        self.strategy_save.pack(side="right")
        self.ttk.Button(footer, text=_tr("恢复默认参数"), style="Small.TButton", command=self.restore_tuning).pack(side="left")
        scroll = ScrollFrame(frame)
        self.strategy_scroll = scroll
        self.build_setting_tuning(scroll.body)
        self.build_categories(scroll.body)
        self.build_model_scores(scroll.body)

    def build_setting_tuning(self, frame):
        tk, ttk = self.tk, self.ttk
        from router_data import STRATEGY_PRESETS

        box = theme.card(frame, padding=STRATEGY_PAD, fill="x")
        ttk.Label(box, text=_tr("可调设置"), style="CardH2.TLabel").pack(anchor="w", pady=(0, 8))
        presets = ttk.Frame(box, style="Card.TFrame")
        presets.pack(fill="x")
        ttk.Label(presets, text=_tr("策略预设"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.preset_buttons = {}
        for name in STRATEGY_PRESETS:
            button = ttk.Button(presets, text=_tr(name), style="Small.TButton",
                                command=lambda n=name: self.apply_strategy_preset(n))
            button.pack(side="left", padx=(0, 6))
            self.preset_buttons[name] = button
        tk.Frame(box, bg=theme.BORDER, height=1).pack(fill="x", pady=12)
        numbers = ttk.Frame(box, style="Card.TFrame")
        numbers.pack(fill="x")
        ttk.Label(numbers, text=_tr("额度阈值"), style="CardH3.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, 8))
        self.tuning_vars = {}
        for row, (key, label) in enumerate((("safety_margin_pct", _tr("周安全余量 %")), ("short_window_floor_pct", _tr("短窗底线 %")),
                                            ("rotate_after_carried_tokens", _tr("上下文轮换阈值"))), start=1):
            ttk.Label(numbers, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            ttk.Entry(numbers, textvariable=var, width=11, font=(self.mono, STRATEGY_FIELD)).grid(
                row=row, column=1, sticky="e", pady=4)
            self.tuning_vars[key] = var
        numbers.columnconfigure(0, weight=1)

        self.prefill(lambda: self.data_module().read_tuning(), self.tuning_loaded)

    def load_strategy(self):
        self.run(lambda: self.data_module().read_tuning(), self.tuning_loaded)
        self.load_scores()

    def tuning_loaded(self, tuning):
        from router_data import STRATEGY_PRESETS

        for key, var in self.tuning_vars.items():
            val = tuning.get(key)
            var.set("" if val is None else str(val))
        active = matching_preset(tuning, STRATEGY_PRESETS)
        for name, button in self.preset_buttons.items():
            button.configure(style="SmallAccent.TButton" if name == active else "Small.TButton")

    def apply_strategy_preset(self, name):
        from router_data import STRATEGY_PRESETS

        name = self.data_module().strategy_preset_key(name)
        preset = STRATEGY_PRESETS.get(name)
        if not preset:
            return
        self.tuning_loaded(preset)
        self.status.set(_tr("已载入预设「{0}」，保存后生效").format(_tr(name)))

    def save_tuning(self):
        values = {key: var.get().strip() for key, var in self.tuning_vars.items() if var.get().strip()}
        self.run(lambda: self.data_module().save_tuning(values), self.say(self.load_strategy))

    def restore_tuning(self):
        from tkinter import messagebox

        if messagebox.askyesno(TITLE, _tr("恢复默认路由参数？"), parent=self.root):
            self.run(lambda: self.data_module().restore_tuning(), self.say(self.load_strategy))

    # ---------- the four categories and the models' scores ----------
    def build_categories(self, frame):
        box = theme.card(frame, padding=STRATEGY_PAD, fill="x", pady=(12, 0))
        self.ttk.Label(box, text=_tr("能力类别与额度经济"), style="CardH2.TLabel").pack(anchor="w")
        self.categories_meta = self.ttk.Label(box, style="CardTiny.TLabel")
        self.categories_meta.pack(anchor="w", pady=(2, 8))
        self.categories_grid = self.ttk.Frame(box, style="Card.TFrame")
        self.categories_grid.pack(fill="x")
        economy = self.ttk.Label(box, style="CardSub.TLabel", justify="left", wraplength=theme.px(640), text=_tr(
            "额度经济：候选的成本 = 在它的约束长周期窗口里，对本单花费 y 积分额度价格 λ(y)；λ 来自该账号的价值阶梯"
            "（在 63 点需求网格上平均）与本周期的需求概率。5 小时窗口只判断能否放下，不计价。"
            "净分 N = 契合 + 连续性 + 临时加成 − 成本。"))
        economy.pack(anchor="w", pady=(10, 0))

    def build_model_scores(self, frame):
        tk, ttk = self.tk, self.ttk
        import score_overview as so

        box = theme.card(frame, padding=STRATEGY_PAD, fill="x", pady=(12, 0))
        ttk.Label(box, text=_tr("模型评分"), style="CardH2.TLabel").pack(anchor="w")
        ttk.Label(box, text=_tr("等级 1 = 面板最弱，2 = 面板中位数，3 = 锚点模型；High 档。✎ 本机覆盖 · ◆ 维护者 · ~ 估算。悬停看证据。"),
                  style="CardTiny.TLabel", wraplength=theme.px(640), justify="left").pack(anchor="w", pady=(2, 8))
        columns = [("model", _tr("模型"), 200, "w")] + [(key, label, 84, "center") for key, label in so.CATEGORIES]
        table_frame, self.scores_table = self.tree(box, columns, 10)
        table_frame.pack(fill="x")
        self.scores_table.bind("<<TreeviewSelect>>", lambda e: self.score_row_selected())
        self.attach_cell_tooltip(self.scores_table)

        editor = ttk.Frame(box, style="Card.TFrame")
        editor.pack(fill="x", pady=(10, 0))
        ttk.Label(editor, text=_tr("手动评分"), style="CardH3.TLabel").grid(row=0, column=0, columnspan=6, sticky="w",
                                                                         pady=(0, 6))
        self.manual_model = ttk.Combobox(editor, state="readonly", width=24)
        self.manual_model.grid(row=1, column=0, sticky="w")
        self.manual_target = ttk.Combobox(editor, state="readonly", width=20)
        self.manual_target.grid(row=1, column=1, sticky="w", padx=(8, 0))
        self.manual_level = tk.StringVar(value="2")
        ttk.Spinbox(editor, from_=1, to=3, increment=0.1, textvariable=self.manual_level, width=5,
                    font=(self.mono, STRATEGY_FIELD)).grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Button(editor, text=_tr("写入本机评分"), style="SmallAccent.TButton",
                   command=self.save_manual_score).grid(row=1, column=3, padx=(8, 0))
        ttk.Button(editor, text=_tr("清除本机评分"), style="Small.TButton",
                   command=self.clear_manual_score).grid(row=1, column=4, padx=(6, 0))
        self.manual_current = ttk.Label(editor, style="CardSub.TLabel", wraplength=theme.px(640), justify="left")
        self.manual_current.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.manual_current._tooltip_text = _tr(
            "本机覆盖只写入你本机的 user-scores.json（与 agentcompass score set 相同的写入路径），不改公开数据。"
            "响应速度的等级 ≥ 2 表示「至少达到面板中位数」。高于 3 的等级按 t3 − t2 外推，显示为 3.0+。")
        self.attach_tooltip(self.manual_current)
        self.manual_model.bind("<<ComboboxSelected>>", lambda e: self.manual_model_changed())
        self.manual_target.bind("<<ComboboxSelected>>", lambda e: self.show_manual_current())
        self.score_data = {}
        self.prefill(lambda: self.data_module().score_overview(), self.scores_loaded)

    def load_scores(self):
        if hasattr(self, "scores_table"):
            self.run(lambda: self.data_module().score_overview(), self.scores_loaded)

    def scores_loaded(self, data):
        import score_overview as so

        self.score_data = data or {}
        self.fill_categories(so)
        if not hasattr(self, "scores_table"):
            return
        table = self.scores_table
        table.delete(*table.get_children())
        self.score_rows = list(self.score_data.get("models") or [])
        for index, row in enumerate(self.score_rows):
            manual = any(c["status"] == "user" for c in row["cells"].values())
            table.insert("", "end", iid=str(index), tags=("win",) if manual else (),
                         values=[row["label"]] + [so.cell_text(row["cells"][key]) for key, _ in so.CATEGORIES])
        labels = [row["label"] for row in self.score_rows]
        self.manual_model.configure(values=labels)
        if self.manual_model.get() not in labels:
            self.manual_model.set(labels[0] if labels else "")
        self.manual_model_changed(keep_target=True)

    def fill_categories(self, so):
        grid = getattr(self, "categories_grid", None)
        if grid is None:
            return
        for child in grid.winfo_children():
            child.destroy()
        data = self.score_data
        self.categories_meta.configure(text=_tr("数据生成于 {0} · 锚点 {1} · 面板 {2} 个模型").format(
            str(data.get("generated_at") or "—")[:10], data.get("anchor") or "—", data.get("panel_n") or "—"))
        ttk = self.ttk
        row = 0
        for category in data.get("categories") or []:
            ttk.Label(grid, text=category["label"], style="CardH3.TLabel").grid(row=row, column=0, sticky="w",
                                                                              pady=(8 if row else 0, 2))
            continuity = category["continuity"]
            cont = ttk.Label(grid, style="CardSub.TLabel", text=_tr("连续性 {0}").format(
                f"{continuity:.3f}" if isinstance(continuity, (int, float)) else "—"))
            cont.grid(row=row, column=1, sticky="w", padx=(10, 0), pady=(8 if row else 0, 2))
            cont._tooltip_text = _tr("连续性（只读，由数据推导）：{0}").format(category["derivation"] or "—")
            self.attach_tooltip(cont)
            ttk.Label(grid, style="CardTiny.TLabel", text=_tr("覆盖：{0}").format(so.coverage_text(category["coverage"]))
                      ).grid(row=row, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(8 if row else 0, 2))
            row += 1
            for facet in category["facets"]:
                name = ttk.Label(grid, text="  " + facet["label"], style="Card.TLabel")
                name.grid(row=row, column=0, sticky="w")
                boards = ttk.Label(grid, text=", ".join(facet["boards"]) or "—", style="CardTiny.TLabel",
                                   wraplength=theme.px(230), justify="left")
                boards.grid(row=row, column=1, sticky="w", padx=(10, 0))
                ttk.Label(grid, text=so.facet_dates_text(facet), style="CardTiny.TLabel").grid(
                    row=row, column=2, sticky="w", padx=(10, 0))
                targets = ttk.Label(grid, text=so.targets_text(facet), style="CardTiny.TLabel", font=(self.mono, 8))
                targets.grid(row=row, column=3, sticky="w", padx=(10, 0))
                targets._tooltip_text = so.targets_tip(facet)
                self.attach_tooltip(targets)
                row += 1
        grid.columnconfigure(1, weight=1)

    def attach_cell_tooltip(self, table):
        """Hover text per cell: the model's category level, status, source and evidence."""
        import score_overview as so

        current = {"cell": None}
        show, hide = self.attach_tooltip(table, bind=False)

        def move(event):
            iid, column = table.identify_row(event.y), table.identify_column(event.x)
            if (iid, column) == current["cell"]:
                return
            hide()
            current["cell"] = (iid, column)
            rows, index = getattr(self, "score_rows", []), int(column.lstrip("#") or 0) - 2
            if not iid or not iid.isdigit() or int(iid) >= len(rows) or not 0 <= index < len(so.CATEGORIES):
                return
            row, key = rows[int(iid)], so.CATEGORIES[index][0]
            table._tooltip_text = so.cell_tip(row["label"], key, row["cells"][key])
            show(event)

        def leave(_event=None):
            current["cell"] = None
            hide()
        table.bind("<Motion>", move, add="+")
        table.bind("<Leave>", leave, add="+")
        table.bind("<Button-1>", leave, add="+")

    def picked_score_row(self):
        label = self.manual_model.get()
        return next((row for row in getattr(self, "score_rows", []) if row["label"] == label), None)

    def score_row_selected(self):
        selected = self.scores_table.selection()
        if selected and selected[0].isdigit() and int(selected[0]) < len(self.score_rows):
            self.manual_model.set(self.score_rows[int(selected[0])]["label"])
            self.manual_model_changed(keep_target=True)

    def manual_model_changed(self, keep_target=False):
        import score_overview as so

        self.manual_targets = {}
        for key, label in so.CATEGORIES:
            for target, facet in so.targets_choices(key):
                self.manual_targets[f"{label} · {facet}"] = target
        values = list(self.manual_targets)
        self.manual_target.configure(values=values)
        if not (keep_target and self.manual_target.get() in values):
            self.manual_target.set(values[0] if values else "")
        self.show_manual_current()

    def manual_selection(self):
        row = self.picked_score_row()
        target = getattr(self, "manual_targets", {}).get(self.manual_target.get())
        return row, target

    def show_manual_current(self):
        import score_overview as so

        row, target = self.manual_selection()
        if row is None or target is None:
            self.manual_current.configure(text="")
            return
        level, status = so.current_manual(row, target)
        self.manual_current.configure(text=_tr("当前 {0}（来源：{1}）").format(so.level_text(level), so.source_label(status)))
        if isinstance(level, (int, float)):
            self.manual_level.set(f"{min(level, 3.0):.1f}")

    def save_manual_score(self):
        row, target = self.manual_selection()
        if row is None or target is None:
            return
        try:
            level = float(self.manual_level.get())
        except ValueError:
            self.status.set(_tr("评分需为数字（1–3）"))
            return
        model = row["model"]
        self.run(lambda: self.data_module().set_manual_score(model, target, level), self.say(self.load_scores))

    def clear_manual_score(self):
        row, target = self.manual_selection()
        if row is None or target is None:
            return
        model = row["model"]
        self.run(lambda: self.data_module().clear_manual_score(model, target), self.say(self.load_scores))

    # system
