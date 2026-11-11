"""Model settings tab "models": account and harness rows, model menus, priority boosts and CLI versions.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr

import time
from pathlib import Path

import ui_theme as theme
from panel_widgets import ScrollFrame, clip, fmt, freeze_redraw, short_badge
import panel_widgets


def selected_chip_layout(widths, available, edit_width, overflow_width=42, gap=6):
    """Largest prefix fitting two compact rows, with overflow and edit controls at its tail."""
    available = max(1, available)
    for count in range(len(widths), -1, -1):
        items = [*widths[:count], *([overflow_width] if count < len(widths) else []), edit_width]
        positions, x, row = [], 0, 0
        for width in items:
            if width > available:
                break
            if x and x + width > available:
                x, row = 0, row + 1
            if row >= 2:
                break
            positions.append((x, row))
            x += width + gap
        else:
            return count, positions, row + 1
    return 0, [(0, 0), (0, 1)], 2


class ModelsTab:
    def build_setting_models(self, frame):
        ttk = self.ttk
        self.build_cli_toolbar(frame)
        # The priority strip owns its one line; the table scrolls inside the remaining space.
        strip = theme.card(frame, padding=(12, 8), fill="x", side="bottom", pady=(8, 0))
        ttk.Label(strip, text=_tr("临时优先加权"), style="Card.TLabel").pack(side="left", padx=(0, 8))
        quick = ttk.Frame(strip, style="Card.TFrame")
        quick.pack(side="left")
        self.fill_priority_buttons(quick, "Tiny.TButton", (0, 4), "+ ")
        ttk.Button(strip, text=_tr("自定义…"), style="Small.TButton", command=self.open_boost_editor).pack(side="right")
        self.boost_chips = ttk.Frame(strip, style="Card.TFrame")
        self.boost_chips.pack(side="left", fill="x", expand=True, padx=(6, 4))
        box = theme.card(frame, padding=(12, 6), fill="both", expand=True, pady=(8, 0))
        titles = ttk.Frame(box, style="Card.TFrame")
        titles.pack(fill="x", pady=(0, 4))
        ttk.Label(titles, text="Harness", style="CardTiny.TLabel", width=19).pack(side="left")
        ttk.Label(titles, text=_tr("账号 / 状态"), style="CardTiny.TLabel", width=20).pack(side="left")
        ttk.Label(titles, text=_tr("已选模型 · 推理档位自动选择"), style="CardTiny.TLabel", width=1).pack(side="left", fill="x", expand=True)
        self.model_scroll = ScrollFrame(box, bg=theme.CARD)
        self.model_rows = self.model_scroll.body
        self.model_rows.columnconfigure(2, weight=1)
        self.model_shape = None
        self.model_widgets = []
        self._catalog_refreshing = False
        self._models_prefilled = False
        self.prefill(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        self.load_models(skip_rows=self._models_prefilled)

    def load_models(self, skip_rows: bool = False):
        if not skip_rows:
            self.run(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        self.load_cli()
        self.load_boost()
        self.refresh_model_availability()

    def refresh_model_availability(self, force=False):
        if getattr(self, "_catalog_refreshing", False) or self.closing:
            return
        self._catalog_refreshing = True
        self.model_refresh_button.state(["disabled"])
        generation = self._generation
        def work():
            try:
                return self.data_module().refresh_model_catalog(force=force)
            except Exception as error:
                panel_widgets.log_error(error)
                return self.data_module().harness_model_rows()
        def done(rows):
            self._catalog_refreshing = False
            self.model_refresh_button.state(["!disabled"])
            self.models_rows_loaded(rows)
            self.root.after(600000, lambda: self.refresh_model_availability() if generation == self._generation else None)
        self.run(work, done, long=True)

    def models_rows_loaded(self, rows):
        self._models_prefilled = True
        self._models_loaded_at = time.monotonic()
        shape = tuple((row["account"], row["harness"]) for row in rows)
        if shape != getattr(self, "model_shape", None):
            with freeze_redraw(self.model_rows):
                for child in self.model_rows.winfo_children():
                    child.destroy()
                self.model_widgets = [self.build_model_row(index, row) for index, row in enumerate(rows)]
                self.model_shape = shape
        self.model_inventory = rows
        for widgets, row in zip(self.model_widgets, rows):
            widgets["on"].set(bool(row["enabled"]))
            theme.set_pill(widgets["pill"], short_badge(row["badge"]), row["kind"])
            widgets["row"] = row
            pattern = row.get("cwd_pattern") or row.get("cwd_regex") or ""
            widgets["cwd"].configure(text="⌁•" if pattern else "⌁")
            widgets["cwd"]._tooltip_text = pattern or _tr("不限目录")
            self.fill_selected_models(widgets, row)
            self.fill_harness_version(widgets, row)
        menu = getattr(self, "model_menu", None)
        if menu is not None and menu.winfo_exists():
            row = next((row for row in rows if (row["account"], row["harness"]) == self.model_menu_key), None)
            if row:
                self.fill_model_menu(row)

    def build_model_row(self, index, row):
        tk, ttk = self.tk, self.ttk
        grid = index * 2
        box = self.model_rows
        if index:
            self.divider(box, row=grid - 1, column=0, columnspan=3, pady=1)
        harness = ttk.Frame(box, style="Card.TFrame")
        harness.grid(row=grid, column=0, sticky="nw", pady=5, padx=(0, 10))
        on = tk.BooleanVar(value=row["enabled"])
        check = ttk.Checkbutton(harness, text=row["harness_label"], variable=on,
                                command=lambda a=row["account"], h=row["harness"], v=on: self.toggle_harness_row(a, h, v.get()))
        check.pack(anchor="w")
        version = ttk.Label(harness, style="CardTiny.TLabel")
        version.pack(anchor="w", padx=(20, 0))
        account = ttk.Frame(box, style="Card.TFrame")
        account.grid(row=grid, column=1, sticky="nw", pady=5, padx=(0, 14))
        name = ttk.Frame(account, style="Card.TFrame")
        name.pack(fill="x")
        ttk.Label(name, text=row["account_label"], style="Card.TLabel").pack(side="left")
        cwd = ttk.Button(name, text="⌁", width=2, style="Tiny.TButton",
                         command=lambda: self.open_account_cwd(row["account"]))
        cwd.pack(side="left")
        self.attach_tooltip(cwd)
        status = ttk.Frame(account, style="Card.TFrame")
        status.pack(anchor="w")
        pill = theme.pill(status, "", "idle")
        pill.pack(side="left")
        if row["harness"] == "grok-bot":
            ttk.Button(status, text="↗", width=2, style="Tiny.TButton", command=lambda: self.show("plugins")).pack(side="left", padx=3)
        selection = ttk.Frame(box, style="Card.TFrame")
        selection.grid(row=grid, column=2, sticky="new", pady=7)
        dropdown = ttk.Button(selection, text="✎ ▾", style="Tiny.TButton")
        dropdown.configure(command=lambda: self.open_model_menu(row["account"], row["harness"], dropdown))
        overflow = ttk.Button(selection, style="Tiny.TButton", command=lambda: dropdown.invoke())
        widgets = {"on": on, "check": check, "version": version, "pill": pill, "chips": selection,
                   "dropdown": dropdown, "overflow": overflow, "cwd": cwd, "row": row, "selected_models": [], "chip_labels": []}
        def resize(event):
            if event.width <= 1 or event.width == widgets.get("_last_chip_width"):
                return
            widgets["_last_chip_width"] = event.width
            pending = getattr(selection, "_chip_after", None)
            if pending:
                selection.after_cancel(pending)
            selection._chip_after = selection.after(60, lambda: self.layout_selected_chips(widgets) if selection.winfo_exists() else None)
        selection.bind("<Configure>", resize)
        selection.bind("<Button-1>", lambda event: (dropdown.invoke(), "break")[1] if event.widget is selection else None)
        return widgets

    def fill_selected_models(self, widgets, row):
        holder = widgets["chips"]
        selected = [model for model in row["models"] if model.get("routed", True) and (model["enabled"] or model["partial"])]
        widgets["selected_models"] = selected
        signature = tuple((model["model"], model["model_label"], model["partial"]) for model in selected)
        if signature != widgets.get("chip_signature"):
            for child in widgets["chip_labels"]:
                child.destroy()
            widgets["chip_labels"] = []
            for model in selected:
                label = self.tk.Label(holder, text=("◐ " if model["partial"] else "") + model["model_label"],
                          bg=theme.ACCENT_SOFT if row["enabled"] else theme.BG,
                          fg=theme.ACCENT if row["enabled"] else theme.FAINT,
                          padx=5, pady=2, font=(theme.FONT, 9))
                label.bind("<Button-1>", lambda event: (widgets["dropdown"].invoke(), "break")[1])
                widgets["chip_labels"].append(label)
            if not selected:
                widgets["chip_labels"].append(self.ttk.Label(holder, text=_tr("未选模型"), style="CardTiny.TLabel"))
            widgets["chip_signature"] = signature
        for label in widgets["chip_labels"]:
            if label.winfo_class() == "Label":
                label.configure(fg=theme.ACCENT if row["enabled"] else theme.FAINT,
                                bg=theme.ACCENT_SOFT if row["enabled"] else theme.BG)
        self.layout_selected_chips(widgets)

    def layout_selected_chips(self, widgets):
        holder = widgets["chips"]
        holder._chip_after = None
        labels, edit, overflow = widgets["chip_labels"], widgets["dropdown"], widgets["overflow"]
        live_w = holder.winfo_width()
        if live_w > 1:
            widgets["_last_chip_width"] = live_w
        width = live_w if live_w > 1 else theme.px(420)
        sizes = [label.winfo_reqwidth() for label in labels]
        overflow.configure(text=f"+{len(labels)}")
        count, positions, rows = selected_chip_layout(sizes, width, edit.winfo_reqwidth(), overflow.winfo_reqwidth(), theme.px(5))
        for label in labels:
            label.place_forget()
        overflow.place_forget()
        height = max([edit.winfo_reqheight(), *(label.winfo_reqheight() for label in labels)], default=theme.px(24))
        controls = labels[:count]
        if count < len(labels):
            overflow.configure(text=f"+{len(labels) - count}")
            controls = [*controls, overflow]
        for widget, (x, row) in zip([*controls, edit], positions):
            widget.place(x=x, y=row * (height + theme.px(3)))
        holder.configure(height=rows * (height + theme.px(3)))
        widgets["visible_chips"] = count

    def close_model_menu(self):
        menu = getattr(self, "model_menu", None)
        if menu is not None and menu.winfo_exists():
            menu.destroy()
        self.model_menu = None

    def open_account_cwd(self, account):
        self.close_model_menu()
        old = getattr(self, "cwd_dialog", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        window = self.cwd_dialog = self.tk.Toplevel(self.root)
        theme.apply(window, _tr("账号目录限制"), "570x310", (520, 290))
        window.transient(self.root)
        width, height = theme.px(570), theme.px(310)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        box = theme.card(window, padding=14, fill="both", expand=True)
        self.ttk.Label(box, text=self.data_module().account_label(account), style="CardH3.TLabel").pack(anchor="w")
        self.ttk.Label(box, text=_tr("同一账号的所有 Harness 共用此限制。留空允许全部目录。"),
                       style="CardTiny.TLabel").pack(anchor="w", pady=(4, 10))
        self.cwd_pattern = self.tk.StringVar(value=self.data_module().read_account_cwd(account))
        self.ttk.Entry(box, textvariable=self.cwd_pattern, font=(self.mono, 10)).pack(fill="x")
        self.ttk.Label(box, text=_tr("路径统一为 /，匹配忽略大小写。"), style="CardTiny.TLabel").pack(anchor="w", pady=(4, 8))
        self.cwd_preview = self.tk.StringVar()
        self.cwd_preview_label = self.ttk.Label(box, textvariable=self.cwd_preview, style="Card.TLabel", justify="left")
        self.cwd_preview_label.pack(anchor="w", fill="x", pady=6)
        buttons = self.ttk.Frame(box, style="Card.TFrame")
        buttons.pack(side="bottom", fill="x")
        self.cwd_save_button = self.ttk.Button(buttons, text=_tr("保存"), style="SmallAccent.TButton",
                                              command=lambda: self.save_account_cwd(account))
        self.cwd_save_button.pack(side="right")
        self.ttk.Button(buttons, text=_tr("取消"), style="Small.TButton", command=window.destroy).pack(side="right", padx=8)
        self.cwd_pattern.trace_add("write", lambda *args: self.update_cwd_preview())
        self.update_cwd_preview()

    def update_cwd_preview(self):
        paths = [str(Path.cwd()), "D:/projects/example", "C:/Temp"]
        try:
            preview = self.data_module().preview_account_cwd(self.cwd_pattern.get(), paths)
            self.cwd_preview.set("\n".join(("✓ " if matches else "— ") + directory for directory, matches in preview))
            self.cwd_preview_label.configure(foreground=theme.TEXT)
            self.cwd_save_button.state(["!disabled"])
        except ValueError as error:
            self.cwd_preview.set(_tr("无效正则：{0}").format(str(error)))
            self.cwd_preview_label.configure(foreground=theme.RED)
            self.cwd_save_button.state(["disabled"])

    def save_account_cwd(self, account):
        pattern = self.cwd_pattern.get()
        def done(message):
            self.status.set(message)
            self.cwd_dialog.destroy()
            self.after_model_change()
        self.run(lambda: self.data_module().save_account_cwd(account, pattern), done)

    def open_model_menu(self, account, harness, anchor):
        self.close_model_menu()
        row = next((row for row in self.model_inventory if (row["account"], row["harness"]) == (account, harness)), None)
        if row is None:
            return
        menu = self.model_menu = self.tk.Toplevel(self.root)
        menu.transient(self.root)
        menu.overrideredirect(True)
        menu.attributes("-topmost", True)
        menu.configure(bg=theme.BORDER, padx=1, pady=1)
        self.model_menu_key = (account, harness)
        width, height = theme.px(380), theme.px(min(360, 95 + 31 * max(1, len(row["models"]))))
        x = min(anchor.winfo_rootx(), self.root.winfo_rootx() + self.root.winfo_width() - width - 8)
        y = anchor.winfo_rooty() + anchor.winfo_height()
        if y + height > self.root.winfo_screenheight():
            y = max(0, anchor.winfo_rooty() - height)
        menu.geometry(f"{width}x{height}+{max(0, x)}+{y}")
        menu.bind("<Escape>", lambda event: self.close_model_menu())
        self.fill_model_menu(row)
        menu.lift()
        menu.focus_set()

    def fill_model_menu(self, row):
        tk, ttk = self.tk, self.ttk
        menu = self.model_menu
        for child in menu.winfo_children():
            child.destroy()
        head = ttk.Frame(menu, style="Card.TFrame", padding=(8, 6))
        head.pack(fill="x")
        self.model_menu_actions = {}
        for action, label in (("all", "全选"), ("invert", "反选"), ("clear", "清空")):
            button = ttk.Button(head, text=_tr(label), style="Tiny.TButton",
                                command=lambda mode=action: self.bulk_model_selection(row["account"], row["harness"], mode))
            button.pack(side="left", padx=(0, 5))
            button.state(["!disabled"] if row["enabled"] and any(m.get("routed", True) for m in row["models"]) else ["disabled"])
            self.model_menu_actions[action] = button
        ttk.Button(head, text="×", width=2, style="Tiny.TButton", command=self.close_model_menu).pack(side="right")
        hint = _tr("监测失败，使用已配置模型") if row.get("models_probe_failed") else (
            _tr("上次检测：{0}").format(time.strftime("%H:%M", time.localtime(row["models_checked_at"])))
            if row.get("models_checked_at") else _tr("后台监测中，先显示已配置模型"))
        ttk.Label(menu, text=hint, style="CardTiny.TLabel", padding=6).pack(side="bottom", fill="x")
        scroll = ScrollFrame(menu, bg=theme.CARD)
        self.model_menu_items = []
        if not row["models"]:
            ttk.Label(scroll.body, text=_tr("尚未发现可用模型"), style="CardTiny.TLabel").pack(anchor="w", padx=10, pady=8)
        for model in sorted(row["models"], key=lambda item: not (item["enabled"] or item["partial"])):
            routed = model.get("routed", True)
            marks = [_tr(tag) for tag in model.get("tags", [])]
            if model.get("availability") == "missing":
                marks.append(_tr("未检测到"))
            if model.get("launch_unavailable"):
                marks.append(model["launch_unavailable"])
            text = model["model_label"] + (" · " + " · ".join(marks) if marks else "")
            var = tk.BooleanVar(value=model["enabled"])
            if model["partial"]:
                # Some efforts enabled: ◐ stands in for the checkbox (ttk's alternate state is invisible under the
                # card theme). Clicking enables every effort; hovering lists which efforts are on and off.
                check = ttk.Label(scroll.body, text="◐ " + text, style="Card.TLabel", cursor="hand2")
                check.pack(anchor="w", fill="x", padx=(12, 10), pady=4)
                if routed and row["enabled"]:
                    check.bind("<Button-1>", lambda _e, m=model["model"]: self.toggle_model(row["account"], row["harness"], m, True), add="+")
                check._tooltip_text = _tr("已启用：{0}；未启用：{1}").format(
                    ", ".join(model.get("efforts_on", [])) or "-", ", ".join(model.get("efforts_off", [])) or "-")
                self.attach_tooltip(check)
                self.model_menu_items.append((model, var, check))
                continue
            check = ttk.Checkbutton(scroll.body, text=text, variable=var,
                                    command=lambda m=model["model"], v=var: self.toggle_model(row["account"], row["harness"], m, v.get()))
            check.pack(anchor="w", fill="x", padx=10, pady=4)
            check.state(["!disabled"] if routed and row["enabled"] else ["disabled"])
            self.model_menu_items.append((model, var, check))

    def bulk_model_selection(self, account, harness, action):
        self.run(lambda: self.data_module().set_harness_models(account, harness, action), self.say(self.after_model_change))

    def fill_harness_version(self, widgets, row):
        provider = {"antigravity-resilient": "agy"}.get(row["harness"], row["harness"])
        item = next((item for item in getattr(self, "cli_inventory", [])
                     if row["account"] in item.get("providers", []) and item["id"].split("#")[0] == provider), None)
        if item is None:
            item = next((item for item in getattr(self, "cli_inventory", []) if item["id"] == provider), {})
        widgets["version"].configure(text=f"v{item['version']}" if item.get("version") else "")

    def after_model_change(self):
        self._cards_cache = None
        self.run(lambda: self.data_module().harness_model_rows(), self.models_rows_loaded)
        if "quota" in self.built:
            self.load_quota(True)

    def toggle_harness_row(self, account, harness, enabled):
        self.run(lambda: self.data_module().toggle_harness(harness, account, enabled), self.say(self.after_model_change))

    def toggle_model(self, account, harness, model, enabled=None):
        def work():
            selected = self.data_module().toggle_harness_model(account, harness, model, enabled)
            return _tr("已启用") if selected else _tr("已停用")
        self.run(work, self.say(self.after_model_change))

    # temporary priority

    def open_boost_editor(self):
        window = getattr(self, "boost_dialog", None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        self.boost_dialog = window = self.tk.Toplevel(self.root)
        theme.apply(window, _tr("设定预期重置时间"), "900x400", (760, 360))
        window.transient(self.root)
        self.build_setting_boost(window)
        self.load_boost()

    def cancel_boost(self, rule):
        import router_priority
        self.run(lambda: router_priority.cancel(rule["id"], rule["revision"]), self.say(self.load_boost))

    def build_setting_boost(self, frame):
        ttk = self.ttk
        box = theme.card(frame, padding=14, fill="both", expand=True)
        quick = ttk.Frame(box, style="Card.TFrame")
        quick.pack(fill="x")
        ttk.Label(quick, text=_tr("一键优先"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.fill_priority_buttons(quick, "Small.TButton", (0, 6))
        form = ttk.Frame(box, style="Card.TFrame")
        form.pack(fill="x", pady=(12, 10))
        ttk.Label(form, text=_tr("设定预期重置时间"), style="CardH3.TLabel").pack(side="left", padx=(0, 10))
        self.boost_target = ttk.Combobox(form, state="readonly", width=24)
        self.boost_target.pack(side="left")
        self.boost_hours = ttk.Combobox(form, state="readonly", width=4, values=["1h", "3h", "6h", "12h", "24h"])
        self.boost_hours.set("6h")
        self.boost_hours.pack(side="left")
        ttk.Button(form, text=_tr("添加"), style="SmallAccent.TButton", command=self.add_boost).pack(side="left", padx=10)
        ttk.Label(box, text=_tr("后重置 · 越近越优先，额度回升后自动撤销"), style="CardTiny.TLabel").pack(fill="x")
        bottom = ttk.Frame(box, style="Card.TFrame")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(bottom, text=_tr("取消所选规则"), style="Small.TButton", command=self.cancel_rule).pack(side="right")
        table, self.rule_table = self.tree(box, (("label", _tr("账号 / 模型"), 180, "w"), ("boost", _tr("加成"), 160, "e"),
                                                 ("until", _tr("预期重置 / 倒计时"), 240, "w"), ("reset", _tr("重置撤销"), 70, "w"),
                                                 ("intent", _tr("来源"), 160, "w")), 6)
        table.pack(fill="both", expand=True)
        data = self.data_module()
        self.prefill(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def load_boost(self):
        self._boost_refreshed = time.monotonic()
        data = self.data_module()
        self.run(lambda: (data.overrides(), data.boost_targets()), self.boost_loaded)

    def boost_loaded(self, value):
        rules, targets = value
        data = self.data_module()
        self.rule_rows = rules
        if hasattr(self, "boost_chips") and self.boost_chips.winfo_exists():
            for child in self.boost_chips.winfo_children():
                child.destroy()
            for rule in rules[:2]:
                label = data.model_label(rule.get("model")) if rule.get("model") else data.account_label(rule.get("account"))
                detail = (f" {rule.get('remaining_hours', 0):.1f}h +{(rule.get('boost') or 0):.2f}"
                          if rule.get('remaining_hours') is not None else "")
                self.ttk.Button(self.boost_chips, text=f"{clip(label, 10)}{detail} ×", style="Tiny.TButton",
                                command=lambda item=rule: self.cancel_boost(item)).pack(side="left", padx=(0, 3))
            if len(rules) > 2:
                self.ttk.Button(self.boost_chips, text=f"+{len(rules) - 2}", style="Tiny.TButton",
                                command=self.open_boost_editor).pack(side="left")
        if not hasattr(self, "rule_table") or not self.rule_table.winfo_exists():
            return
        self.rule_table.delete(*self.rule_table.get_children())
        for index, rule in enumerate(rules):
            self.rule_table.insert("", "end", iid=str(index), values=(
                rule["label"], fmt(rule["boost"], 2, True),
                f"{rule['until']} · {rule.get('countdown', '')}",
                _tr("是") if rule["stop_on_reset"] else _tr("否"), rule["intent"]))
        if not rules:
            self.rule_table.insert("", "end", values=(_tr("当前没有生效规则"), "", "", "", ""), tags=("muted",))
        self.boost_pairs = [(a, None) for a in dict.fromkeys(a for a, _ in targets)] + targets
        self.boost_target.configure(values=[
            f"{data.account_label(a)} / {data.model_label(m) if m else _tr('全部模型')}" for a, m in self.boost_pairs])
        if not self.boost_target.get() and self.boost_pairs:
            self.boost_target.current(0)

    def fill_priority_buttons(self, frame, style, padx, prefix=""):
        """One-click reset-priority buttons for the targets the router derives from this fleet."""
        import router_priority

        def work():
            allocator, _ = router_priority.modules()
            return router_priority.targets(allocator, allocator.load_accounts())

        def done(targets):
            for target in targets or ():
                self.ttk.Button(frame, text=f"{prefix}{target['label']}", style=style,
                                command=lambda a=target["account"], m=target["model"]: self.enable_priority(a, m)
                                ).pack(side="left", padx=padx)
        self.run(work, done)

    def enable_priority(self, account, model):
        import router_priority

        def work():
            allocator, _ = router_priority.modules()
            accounts = allocator.load_accounts()
            target = next((t for t in router_priority.targets(allocator, accounts)
                           if t["account"] == account and t["model"] == model), None)
            if target is None:
                return _tr("该模型当前不可设为优先")
            return router_priority.perform({"kind": "enable", **{k: target[k] for k in ("account", "model", "window")}})
        self.run(work, self.say(self.load_boost))

    def add_boost(self):
        index = self.boost_target.current()
        if index < 0:
            self.status.set(_tr("先选择账号 / 模型"))
            return
        account, model = self.boost_pairs[index]
        hours = int(self.boost_hours.get().rstrip("h"))
        self.run(lambda: self.data_module().add_boost(account, model, hours=hours), self.say(self.load_boost))

    def cancel_rule(self):
        selected = self.rule_table.selection()
        if not selected or not getattr(self, "rule_rows", None) or not selected[0].isdigit():
            self.status.set(_tr("先选择一条规则"))
            return
        rule = self.rule_rows[int(selected[0])]
        import router_priority

        self.run(lambda: router_priority.cancel(rule["id"], rule["revision"]), self.say(self.load_boost))

    # strategy & parameters

    def build_cli_toolbar(self, frame):
        import cli_updates
        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x")
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(12, 8))
        inner.pack(side="left", fill="both", expand=True)
        self.cli_pill = theme.pill(inner, _tr("检测中"), "idle")
        self.cli_pill.pack(side="left", padx=(0, 8))
        self.cli_status = tk.StringVar(value=_tr("正在读取 CLI 版本…"))
        self.cli_status_label = ttk.Label(inner, textvariable=self.cli_status, style="Card.TLabel", width=1)
        self.cli_status_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cli_update_button = ttk.Button(inner, text=_tr("检查更新"), style="SmallAccent.TButton", command=self.update_all_cli)
        self.cli_update_button.pack(side="right", padx=(6, 0))
        self.model_refresh_button = ttk.Button(inner, text=_tr("刷新模型"), style="Small.TButton",
                                               command=lambda: self.refresh_model_availability(force=True))
        self.model_refresh_button.pack(side="right", padx=(6, 0))
        self.cli_progress_button = ttk.Button(inner, text=_tr("更新进度"), style="Small.TButton", command=self.open_updates)
        self.cli_progress_button.pack(side="right", padx=(6, 0))
        self.cli_log_button = ttk.Button(inner, text=_tr("更新日志"), style="Small.TButton", command=self.open_cli_log)
        self.cli_log_button.pack(side="right", padx=(6, 0))
        self.cli_auto = tk.BooleanVar(value=cli_updates.get_auto_update())
        self.cli_auto_check = ttk.Checkbutton(inner, text=_tr("自动更新（每天）"), variable=self.cli_auto,
                                              command=self.set_cli_auto_update)
        self.cli_auto_check.pack(side="right", padx=(0, 6))
        self.cli_updating = False
        self.prefill(lambda: cli_updates.snapshot(probe=False), self.cli_loaded)

    def open_cli_log(self):
        import cli_updates
        self.run(cli_updates.open_log, self.say())

    def open_update_log(self):
        import update_status
        self.run(update_status.open_log, self.say())

    def load_cli(self, summary=None):
        import cli_updates
        self._cli_generation = getattr(self, "_cli_generation", 0) + 1
        generation = self._cli_generation
        def done(rows):
            if generation == self._cli_generation:
                self.cli_loaded(rows)
                if summary is not None:
                    self.cli_status.set(summary)
        self.run(cli_updates.snapshot, done)

    def cli_loaded(self, rows):
        self.cli_inventory = rows
        installed = sum(bool(row.get("path")) and row.get("source") not in ("missing", "custom") for row in rows)
        available = sum(row.get("status") == "outdated" for row in rows)
        updatable = [row for row in rows if row.get("can_update", row.get("source") in ("npm", "native"))]
        unchecked = bool(updatable) and all(row.get("status") == "unchecked" for row in updatable)
        if hasattr(self, "cli_pill"):
            if getattr(self, "cli_updating", False):
                theme.set_pill(self.cli_pill, _tr("更新中"), "warn")
            elif available:
                theme.set_pill(self.cli_pill, _tr("{0} 可更新").format(available), "warn")
            elif unchecked:
                theme.set_pill(self.cli_pill, _tr("待检查"), "idle")
            else:
                theme.set_pill(self.cli_pill, _tr("已是最新"), "ok")
        if not getattr(self, "cli_updating", False):
            self.cli_status.set(_tr("CLI：{0} 已安装 · 尚未检查更新").format(installed) if unchecked and not available else
                                _tr("CLI：{0} 已安装 · {1} 可更新").format(installed, available))
        for widgets, row in zip(getattr(self, "model_widgets", []), getattr(self, "model_inventory", [])):
            self.fill_harness_version(widgets, row)

    def set_cli_auto_update(self):
        import cli_updates
        enabled = bool(self.cli_auto.get())
        self.run(lambda: cli_updates.set_auto_update(enabled), lambda value: self.status.set(
            _tr("已开启每日 CLI 自动更新") if value else _tr("已关闭 CLI 自动更新")))

    def update_all_cli(self):
        import cli_updates
        if self.cli_updating:
            return
        self.cli_updating = True
        self.cli_update_button.state(["disabled"])
        self.cli_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "cli_pill"):
            theme.set_pill(self.cli_pill, _tr("更新中"), "warn")
        self.cli_status.set(_tr("正在检查并更新 CLI…"))
        self._open_update_window_if_interactive()
        generation = self._generation
        def progress(message):
            # Use the panel's event queue; Tk must only be touched on its UI thread.
            self._post_ui_progress(generation, self.cli_progress, message)
        def work():
            try:
                return cli_updates.update_all(progress=progress)
            except Exception as error:
                return {"summary": _tr("CLI 更新失败：{0}").format(str(error)), "updated": [], "failed": ["batch"]}
        def done(result):
            self.cli_updating = False
            self.cli_update_button.state(["!disabled"])
            self.cli_update_button.configure(text=_tr("检查更新"))
            if hasattr(self, "cli_pill"):
                failed = bool(result.get("failed") or result.get("deferred"))
                theme.set_pill(self.cli_pill, _tr("部分异常") if failed else _tr("已是最新"), "warn" if failed else "ok")
            self.cli_status.set(result["summary"])
            self.status.set(result["summary"])
            self.load_cli(summary=result["summary"])
        self.run(work, done, _tr("正在检查并更新 CLI…"), long=True)

    def cli_progress(self, message):
        self.cli_status.set(message)
        self.status.set(message)
