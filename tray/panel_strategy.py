"""Model settings tab "strategy": quota thresholds and tuning presets.

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

    def build_setting_tuning(self, frame):
        tk, ttk = self.tk, self.ttk
        from router_data import STRATEGY_PRESETS

        box = theme.card(frame, padding=STRATEGY_PAD, fill="x")
        ttk.Label(box, text=_tr("策略与阈值"), style="CardH2.TLabel").pack(anchor="w", pady=(0, 8))
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

    # system
