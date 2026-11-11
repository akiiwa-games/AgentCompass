"""Model settings tab "system": AgentCompass version, scorer and system options.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr, tr

import ui_theme as theme
import i18n
from panel_widgets import ScrollFrame
import panel_widgets


class SystemTab:
    def build_setting_system(self, frame):
        tk, ttk = self.tk, self.ttk
        scroll = ScrollFrame(frame)
        frame = scroll.body
        self.build_version_card(frame)
        box = theme.card(frame, padding=14, fill="x")
        box.columnconfigure(1, weight=1)
        rows = ((_tr("开机自启"), _tr("AgentCompass 随 Windows 启动")), (_tr("宿主 Skill"), _tr("Claude / Codex 的 agent-compass 链接")),
                (_tr("Dot 中转库"), _tr("GitHub 仓库 owner/name")))
        for row, (title, hint) in enumerate(rows):
            if row:
                self.divider(box, row=row * 2 - 1, column=0, columnspan=3, pady=8)
            names = ttk.Frame(box, style="Card.TFrame")
            names.grid(row=row * 2, column=0, sticky="w")
            ttk.Label(names, text=title, style="CardH3.TLabel").pack(anchor="w")
            ttk.Label(names, text=hint, style="CardTiny.TLabel").pack(anchor="w")
        self.autostart_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text=_tr("开启"), variable=self.autostart_var,
                        command=self.toggle_tray_autostart).grid(row=0, column=2, sticky="e")
        ttk.Button(box, text=_tr("同步链接"), style="Small.TButton", command=self.sync_host_skills).grid(row=2, column=2, sticky="e")
        repo = ttk.Frame(box, style="Card.TFrame")
        repo.grid(row=4, column=1, columnspan=2, sticky="e")
        self.dot_repo_var = tk.StringVar()
        ttk.Entry(repo, textvariable=self.dot_repo_var, width=28, font=(self.mono, 9)).pack(side="left")
        ttk.Button(repo, text=_tr("保存"), style="Small.TButton", command=self.save_dot_repo).pack(side="left", padx=(6, 0))
        self.divider(box, row=5, column=0, columnspan=3, pady=8)
        ttk.Label(box, text=tr("界面语言", "Language"), style="CardH3.TLabel").grid(row=6, column=0, sticky="w")
        self.language_box = ttk.Combobox(box, state="readonly", values=("简体中文", "English"), width=18)
        self.language_box.set("English" if i18n.is_en() else "简体中文")
        self.language_box.grid(row=6, column=2, sticky="e")
        self.language_box.bind("<<ComboboxSelected>>",
                               lambda event: self.change_language("en" if self.language_box.get() == "English" else "zh"))
        import cli_updates
        ttk.Button(box, text=_tr("查看 CLI 日志"), style="Small.TButton",
                   command=lambda: self.run(cli_updates.open_log, self.say())).grid(row=6, column=1, sticky="e", padx=8)
        self.build_scorer_card(frame)
        self._system_prefilled = False
        self.prefill(self._read_system_fast, self._system_fast_loaded)
        self.load_system(skip_fast=self._system_prefilled)

    def build_version_card(self, frame):
        import updates

        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x", pady=(0, 12))
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(14, 10))
        inner.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(inner, style="Card.TFrame")
        top.pack(fill="x")
        local_ver = updates.local_version()
        self.ac_version_var = tk.StringVar(value=f"AgentCompass {local_ver}")
        self.ac_version_label = ttk.Label(top, textvariable=self.ac_version_var, style="CardH3.TLabel")
        self.ac_version_label.pack(side="left")
        self.ac_version_pill = theme.pill(top, _tr("检测中"), "idle")
        self.ac_version_pill.pack(side="left", padx=(8, 0))
        self.ac_update_button = ttk.Button(
            top, text=_tr("更新 AgentCompass"), style="SmallAccent.TButton", command=self.update_agentcompass_now
        )
        self.ac_update_button.pack(side="right", padx=(6, 0))
        self.ac_check_update_button = ttk.Button(
            top, text=_tr("检查更新"), style="Small.TButton", command=lambda: self.check_agentcompass_update(force=True)
        )
        self.ac_check_update_button.pack(side="right", padx=(6, 0))
        self.ac_update_progress_button = ttk.Button(
            top, text=_tr("更新进度"), style="Small.TButton", command=self.open_updates
        )
        self.ac_update_progress_button.pack(side="right", padx=(6, 0))
        self.ac_version_detail_var = tk.StringVar(value=_tr("正在读取安装状态…"))
        self.ac_version_detail_label = ttk.Label(
            inner,
            textvariable=self.ac_version_detail_var,
            style="CardTiny.TLabel",
            wraplength=theme.px(680),
            justify="left",
        )
        self.ac_version_detail_label.pack(anchor="w", pady=(4, 0))
        self.ac_updating = False
        self.ac_version_info = None

    def ac_version_loaded(self, status: dict):
        if not isinstance(status, dict):
            return
        self.ac_version_info = status
        display_ver = status.get("display_version") or f"AgentCompass {status.get('tray_version', '0.0.0')}"
        if hasattr(self, "ac_version_var"):
            self.ac_version_var.set(display_ver)
        if hasattr(self, "ac_version_pill") and not getattr(self, "ac_updating", False):
            if status.get("update_available"):
                theme.set_pill(self.ac_version_pill, status.get("update_label") or _tr("可更新"), "warn")
            elif status.get("version_mismatch"):
                theme.set_pill(self.ac_version_pill, _tr("版本不一致"), "warn")
            elif not status.get("ok", True):
                theme.set_pill(self.ac_version_pill, _tr("检查失败"), "warn")
            else:
                theme.set_pill(self.ac_version_pill, _tr("已是最新"), "ok")
        if hasattr(self, "ac_version_detail_var") and not getattr(self, "ac_updating", False):
            shape_map = {
                "git": _tr("Git 仓库"),
                "installer": _tr("安装包"),
                "wheel": _tr("Wheel 包"),
            }
            shape_text = shape_map.get(status.get("shape"), status.get("shape") or "")
            parts = []
            if shape_text:
                parts.append(_tr("安装形态：{0}").format(shape_text))
            if status.get("status_line"):
                parts.append(str(status["status_line"]))
            installs = status.get("installs") or {}
            for r_info in installs.get("separate_routers") or ():
                r_tool = r_info.get("installer") or r_info.get("shape") or "router"
                parts.append(f"Router ({r_tool} {r_info.get('version')}): {r_info.get('root')}")
            self.ac_version_detail_var.set(" · ".join(parts))

    def check_agentcompass_update(self, force: bool = False):
        import updates

        if getattr(self, "ac_updating", False):
            return
        if hasattr(self, "ac_check_update_button") and force:
            self.ac_check_update_button.state(["disabled"])

        def work():
            return updates.version_status(force=force, network=True)

        def done(status):
            if hasattr(self, "ac_check_update_button"):
                self.ac_check_update_button.state(["!disabled"])
            self.ac_version_loaded(status)
            if force and isinstance(status, dict) and status.get("status_line"):
                self.status.set(status["status_line"])

        self.run(work, done, _tr("正在检查 AgentCompass 更新…") if force else None)

    def update_agentcompass_now(self):
        import update_status
        import updates

        if getattr(self, "ac_updating", False):
            return
        self.ac_updating = True
        if hasattr(self, "ac_update_button"):
            self.ac_update_button.state(["disabled"])
            self.ac_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "ac_version_pill"):
            theme.set_pill(self.ac_version_pill, _tr("更新中"), "warn")
        if hasattr(self, "ac_version_detail_var"):
            self.ac_version_detail_var.set(_tr("正在更新 AgentCompass…"))
        self._open_update_window_if_interactive()
        generation = self._generation

        def progress(event):
            update_status.record(event)
            msg = event.get("message", "") if isinstance(event, dict) else str(event)
            if msg:
                self._post_ui_progress(generation, self._ac_update_progress, msg)

        def work():
            try:
                summary = updates.update_agentcompass(progress=progress)
                return {"ok": True, "summary": summary, "status": updates.version_status(force=False, network=False)}
            except Exception as error:
                return {
                    "ok": False,
                    "summary": _tr("AgentCompass 更新失败：{0}").format(error),
                    "status": updates.version_status(force=False, network=False),
                }

        def done(result):
            self.ac_updating = False
            if hasattr(self, "ac_update_button"):
                self.ac_update_button.state(["!disabled"])
                self.ac_update_button.configure(text=_tr("更新 AgentCompass"))
            if isinstance(result.get("status"), dict):
                self.ac_version_loaded(result["status"])
            summary = result.get("summary") or ""
            if not result.get("ok") and hasattr(self, "ac_version_pill"):
                theme.set_pill(self.ac_version_pill, _tr("更新失败"), "warn")
            if summary:
                if hasattr(self, "ac_version_detail_var"):
                    self.ac_version_detail_var.set(summary)
                self.status.set(summary)

        self.run(work, done, _tr("正在更新 AgentCompass…"), long=True)

    def _ac_update_progress(self, message: str):
        if hasattr(self, "ac_version_detail_var"):
            self.ac_version_detail_var.set(message)
        self.status.set(message)

    def build_scorer_card(self, frame):
        tk, ttk = self.tk, self.ttk
        data = self.data_module()
        box = theme.card(frame, padding=14, fill="x", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        head = ttk.Frame(box, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(head, text=_tr("路由分类小模型（System-One Scorer）"), style="CardH3.TLabel").pack(anchor="w")
        ttk.Label(head, text=_tr("route_judge 用它给任务分类打分；失败时回退到 Gemini Flash，再回退到离线规则"),
                  style="CardTiny.TLabel", wraplength=theme.px(630), justify="left").pack(anchor="w")
        self.scorer_types = data.SCORER_TYPES
        self.scorer_config = {}
        self.scorer_type_box = ttk.Combobox(box, state="readonly", width=38, values=[row[1] for row in self.scorer_types])
        self.scorer_key_var, self.scorer_url_var, self.scorer_model_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.scorer_key_entry = ttk.Entry(box, textvariable=self.scorer_key_var, show="•", font=(self.mono, 9))
        self.scorer_key_toggle = ttk.Button(box, text=_tr("显示"), style="Small.TButton", width=5, command=self.toggle_scorer_key)
        self.scorer_url_entry = ttk.Entry(box, textvariable=self.scorer_url_var, font=(self.mono, 9))
        self.scorer_model_entry = ttk.Entry(box, textvariable=self.scorer_model_var, font=(self.mono, 9))
        fields = ((_tr("小模型类型"), self.scorer_type_box), (_tr("API Key（Jev / 自定义）"), self.scorer_key_entry),
                  (_tr("接口地址（可选）"), self.scorer_url_entry), (_tr("模型名（可选）"), self.scorer_model_entry))
        for row, (title, widget) in enumerate(fields, start=1):
            ttk.Label(box, text=title, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
        self.scorer_key_toggle.grid(row=2, column=2, sticky="e", padx=(6, 0))
        self.scorer_type_box.bind("<<ComboboxSelected>>", lambda e: self.scorer_type_changed())
        bottom = ttk.Frame(box, style="Card.TFrame")
        bottom.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.scorer_hint = ttk.Label(bottom, text="", style="CardTiny.TLabel", wraplength=theme.px(420), justify="left")
        self.scorer_hint.pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text=_tr("保存小模型配置"), style="SmallAccent.TButton", command=self.save_scorer).pack(side="right")
        self.scorer_test_button = ttk.Button(bottom, text=_tr("测试打分"), style="Small.TButton", command=self.test_scorer)
        self.scorer_test_button.pack(side="right", padx=(0, 8))
        self.scorer_result = ttk.Label(box, text="", style="CardTiny.TLabel", wraplength=theme.px(560), justify="left")
        self.scorer_result.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def selected_scorer(self):
        label = self.scorer_type_box.get()
        return next((row for row in self.scorer_types if row[1] == label), self.scorer_types[0])

    def scorer_type_changed(self):
        type_id, _, needs_key, takes_url, hint = self.selected_scorer()
        field = (self.scorer_config.get("fields") or {}).get(type_id) or {}
        self.scorer_key_var.set(field.get("api_key_masked") or "")
        self.scorer_url_var.set(field.get("base_url") or "")
        self.scorer_model_var.set(field.get("model") or "")
        self.scorer_key_entry.configure(state="normal" if needs_key else "disabled")
        self.scorer_key_toggle.configure(state="normal" if needs_key else "disabled")
        for entry in (self.scorer_url_entry, self.scorer_model_entry):
            entry.configure(state="normal" if takes_url else "disabled")
        notes = [hint]
        if field.get("key_hint"):
            notes.append(field["key_hint"])
        if self.scorer_config.get("type_from_env"):
            notes.append(_tr('注意：环境变量 {0} 覆盖了此处的选择').format(f"{self.scorer_config['type_from_env']}"))
        self.scorer_hint.configure(text="；".join(notes))

    def scorer_loaded(self, config):
        self.scorer_config = config or {}
        type_id = self.scorer_config.get("scorer_type")
        label = next((row[1] for row in self.scorer_types if row[0] == type_id), self.scorer_types[0][1])
        self.scorer_type_box.set(label)
        self.scorer_type_changed()
        if self.scorer_config.get("config_error"):
            self.scorer_result.configure(text=_tr('配置有误：{0}').format(f"{self.scorer_config['config_error']}"))

    def toggle_scorer_key(self):
        hidden = self.scorer_key_entry.cget("show") != ""
        self.scorer_key_entry.configure(show="" if hidden else "•")
        self.scorer_key_toggle.configure(text=_tr("隐藏") if hidden else _tr("显示"))

    def scorer_form(self):
        type_id, _, needs_key, takes_url, _ = self.selected_scorer()
        return (type_id, self.scorer_key_var.get() if needs_key else None,
                self.scorer_url_var.get() if takes_url else "", self.scorer_model_var.get() if takes_url else "")

    def save_scorer(self):
        type_id, key, url, model = self.scorer_form()
        data = self.data_module()

        def work():
            message = data.save_scorer_config(type_id, key, url, model)
            return message, data.read_scorer_config()

        def done(value):
            message, config = value
            self.scorer_loaded(config)
            self.scorer_result.configure(text=message)
            self.status.set(message)
        self.run(work, done, _tr("正在保存小模型配置…"))

    def test_scorer(self):
        type_id, key, url, model = self.scorer_form()
        self.scorer_test_button.configure(state="disabled", text=_tr("测试中…"))
        self.scorer_result.configure(text=_tr("正在用示例任务测试打分…"))

        def work():
            try:
                return self.data_module().test_scorer_config(type_id, key, url or None, model or None)
            except Exception as exc:
                return _tr('测试失败：{0}').format(f'{exc}')

        def done(message):
            self.scorer_test_button.configure(state="normal", text=_tr("测试打分"))
            self.scorer_result.configure(text=message)
            self.status.set(message)
        self.run(work, done)

    def _read_scorer_safe(self):
        try:
            return self.data_module().read_scorer_config()
        except Exception as exc:
            panel_widgets.log_error(exc)
            return {"config_error": _tr('读取失败：{0}').format(f'{exc}')}

    def _read_version_safe(self, network: bool = False):
        import updates

        try:
            return updates.version_status(force=False, network=network)
        except Exception as exc:
            panel_widgets.log_error(exc)
            ver = updates.local_version()
            return {"ok": False, "tray_version": ver, "display_version": f"AgentCompass {ver}", "status_line": str(exc)}

    def _read_system_fast(self):
        return self.data_module().dot_details(), self._read_scorer_safe(), self._read_version_safe(network=False)

    def _system_fast_loaded(self, value):
        self._system_prefilled = True
        dot, scorer = value[0], value[1]
        self.dot_repo_var.set(dot.get("transfer_repo") or "")
        self.scorer_loaded(scorer)
        if len(value) > 2:
            self.ac_version_loaded(value[2])

    def load_system(self, skip_fast: bool = False):
        import deploy

        if skip_fast:
            self.run(
                lambda: (deploy.startup_enabled(), self._read_version_safe(network=True)),
                lambda pair: (self.autostart_var.set(bool(pair[0])), self.ac_version_loaded(pair[1])),
            )
        else:
            self.run(
                lambda: (deploy.startup_enabled(), self.data_module().dot_details(), self._read_scorer_safe(), self._read_version_safe(network=True)),
                self.system_loaded,
            )

    def system_loaded(self, value):
        startup_on = value[0]
        self.autostart_var.set(bool(startup_on))
        self._system_fast_loaded(value[1:])

    def toggle_tray_autostart(self):
        import deploy

        on = bool(self.autostart_var.get())

        def work():
            if on:
                deploy.write_startup()
                return _tr("已开启 AgentCompass 开机自启")
            deploy.remove_startup()
            return _tr("已关闭 AgentCompass 开机自启")
        self.run(work, self.say())

    def sync_host_skills(self):
        import deploy

        self.run(lambda: "；".join(deploy.link_skills() or [_tr("宿主 Skill 链接均已就绪")]), self.say())

    def save_dot_repo(self):
        repo = self.dot_repo_var.get()
        self.run(lambda: self.data_module().save_dot_repo(repo), self.say())

    # CLI updates on the model settings home
