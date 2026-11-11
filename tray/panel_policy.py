"""The model settings page: the segmented switch between its three tabs.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr

import time

import page_stack
import ui_theme as theme
from panel_widgets import SETTINGS, TRACK


SETTING_ALIASES = {"accounts": "models", "boost": "models", "cli": "models", "tuning": "strategy"}


class PolicyPage:
    # ---------- page 3: model settings ----------
    def build_policy(self, page):
        ttk = self.ttk
        bar = self.header(page, _tr("模型设置"))
        self.setting_switch = theme.Segmented(bar, SETTINGS, self.show_setting, track=TRACK)
        self.setting_switch.frame.pack(side="right")
        self.policy_stack = ttk.Frame(page)
        self.policy_stack.pack(fill="both", expand=True)
        self.policy_stack.pack_propagate(False)
        self.setting_stack = page_stack.PageStack(self.policy_stack)
        self.setting_frames = {key: ttk.Frame(self.policy_stack) for key, _ in SETTINGS}
        self.settings_built = set()
        self.current_setting = None
        self.show_setting(getattr(self, "_initial_setting", None) or "models")

    def show_setting(self, key):
        key = SETTING_ALIASES.get(key, key)
        if key != getattr(self, "current_setting", None):
            self.close_model_menu()
        if "policy" not in self.built:
            self.show("policy")
        if key not in self.setting_frames or key == self.current_setting:
            return
        self.setting_switch.set(key)
        revisit = key in self.settings_built
        if not revisit:
            self.settings_built.add(key)
            getattr(self, f"build_setting_{key}")(self.setting_frames[key])
        self.current_setting = key
        self.setting_stack.show(self.setting_frames[key])
        if revisit and key == "models":
            if time.monotonic() - getattr(self, "_models_loaded_at", 0.0) >= 5.0:
                self.load_models()

    open_setting = show_setting

    def load_policy(self):
        for key in getattr(self, "settings_built", ()):
            getattr(self, f"load_{key}")()
