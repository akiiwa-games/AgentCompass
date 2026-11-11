"""The integrations page: plugin cards, toggles and component updates.

A mixin of router_window.Panel; methods run on the panel instance.
"""
from __future__ import annotations

from i18n import translate as _tr

import os
import threading
import time
from pathlib import Path

import plugins
import ui_theme as theme
from panel_widgets import ScrollFrame
import panel_widgets


def follow_width(container, label, margin: int, floor: int) -> None:
    """Wrap label to container's width; only real width changes re-wrap, so a re-wrap cannot re-trigger itself."""
    last = [None]

    def changed(event):
        if event.width <= 1:
            return
        if last[0] is None or abs(event.width - last[0]) > 2:
            last[0] = event.width
            label.configure(wraplength=max(theme.px(floor), event.width - margin))
    container.bind("<Configure>", changed)


class PluginsPage:
    # ---------- page 4: integrations ----------
    def build_plugins_toolbar(self, frame):
        tk, ttk = self.tk, self.ttk
        bar = theme.card(frame, padding=0, fill="x", pady=(0, 12))
        tk.Frame(bar, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        inner = ttk.Frame(bar, style="Card.TFrame", padding=(12, 8))
        inner.pack(side="left", fill="both", expand=True)
        self.plugins_pill = theme.pill(inner, _tr("检测中"), "idle")
        self.plugins_pill.pack(side="left", padx=(0, 8))
        self.plugins_status = tk.StringVar(value=_tr("正在读取集成状态…"))
        self.plugins_status_label = ttk.Label(inner, textvariable=self.plugins_status, style="Card.TLabel", width=1)
        self.plugins_status_label.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.plugins_update_button = ttk.Button(inner, text=_tr("检查更新"), style="SmallAccent.TButton",
                                                command=self.update_components)
        self.plugins_update_button.pack(side="right", padx=(6, 0))
        self.plugins_progress_button = ttk.Button(inner, text=_tr("更新进度"), style="Small.TButton",
                                                  command=self.open_updates)
        self.plugins_progress_button.pack(side="right", padx=(6, 0))
        self.plugins_log_button = ttk.Button(inner, text=_tr("更新日志"), style="Small.TButton",
                                             command=self.open_update_log)
        self.plugins_log_button.pack(side="right", padx=(6, 0))
        self.plugins_updating = False

    def build_plugins(self, page):
        tk, ttk = self.tk, self.ttk
        self.header(page, _tr("外部集成"), (
            (_tr("重启服务"), self.restart_services, "Small.TButton"),
            (_tr("刷新"), self.load_plugins, "SmallAccent.TButton"),
        ))
        self.build_plugins_toolbar(page)

        scroll = ScrollFrame(page)
        grid = scroll.body
        grid.columnconfigure(0, weight=1, uniform="plugins")
        grid.columnconfigure(1, weight=1, uniform="plugins")
        config = plugins.load()
        self.plugin_widgets = {}
        for index, plugin in enumerate(plugins.PLUGINS):
            box = theme.card(grid, padding=0)
            box.outer.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0 if index % 2 == 0 else 12, 0),
                           pady=(0, 12))
            tk.Frame(box, bg=plugin.color, width=4).pack(side="left", fill="y")
            inner = ttk.Frame(box, style="Card.TFrame", padding=(12, 10))
            inner.pack(side="left", fill="both", expand=True)
            top = ttk.Frame(inner, style="Card.TFrame")
            top.pack(fill="x")
            ttk.Label(top, text=plugin.name, style="CardH3.TLabel").pack(side="left")
            ttk.Label(top, text=plugin.description, style="CardTiny.TLabel").pack(side="left", padx=(8, 0))
            status = theme.pill(top, _tr("检测中"), "idle")
            status.pack(side="right")
            switches = ttk.Frame(inner, style="Card.TFrame")
            switches.pack(fill="x", pady=(6, 0))
            enabled = tk.BooleanVar(value=config[plugin.id]["enabled"])
            autostart = tk.BooleanVar(value=config[plugin.id]["autostart"])
            if plugin.id != "grok-bot":  # CLI account enablement belongs to routing settings, not a daemon toggle.
                ttk.Checkbutton(switches, text=_tr("启用"), variable=enabled,
                                command=lambda p=plugin.id, v=enabled: self.toggle_enabled(p, v.get())).pack(side="left")
                ttk.Checkbutton(switches, text=_tr("随托盘自启"), variable=autostart,
                                command=lambda p=plugin.id, v=autostart: self.toggle_autostart(p, v.get())).pack(
                    side="left", padx=12)
            info = tk.StringVar(value="")
            info_label = ttk.Label(inner, textvariable=info, style="CardTiny.TLabel")
            info_label.pack(fill="x", pady=(4, 0))
            follow_width(inner, info_label, 24, 160)
            actions = ttk.Frame(inner, style="Card.TFrame")
            actions.pack(fill="x", pady=(8, 0))
            for label, work in self.plugin_actions(plugin.id):
                ttk.Button(actions, text=label, style="Tiny.TButton",
                           command=lambda w=work, p=plugin.id: self.plugin_act(p, w)).pack(side="left", padx=(0, 4))
            if plugin.id == "dot2api":
                ttk.Button(actions, text=_tr("复制 URL"), style="Tiny.TButton", command=self.copy_tunnel).pack(side="left")
            elif plugin.id == "muse2api":
                ttk.Button(actions, text=_tr("复制 API Key"), style="Tiny.TButton",
                           command=self.copy_muse_key).pack(side="left")
            if plugin.id == "muse2api":
                ttk.Button(actions, text=_tr("登录导入账号"), style="Tiny.TButton",
                           command=lambda p=plugin.id: self.import_account(p)).pack(side="left", padx=(4, 0))
            self.plugin_widgets[plugin.id] = {"status": status, "info": info, "enabled": enabled, "autostart": autostart}
        self.prefill(self._prefill_plugins_info, lambda _v: None)
        self.load_plugins()

    def _prefill_plugins_info(self):
        for plugin in plugins.PLUGINS:
            widgets = self.plugin_widgets.get(plugin.id)
            if not widgets:
                continue
            deployed = (plugins.plugin_root(plugin.id) / ".git").exists()
            if plugin.id == "grok-bot":
                import grok_bot_cli
                widgets["info"].set(grok_bot_cli.info())
            elif plugin.id == "dot2api":
                dot = self.data_module().dot_details()
                self.tunnel_url = dot.get("tunnel_url") or ""
                widgets["info"].set(
                    (self.tunnel_url or _tr("隧道未发布"))
                    if (deployed or self.tunnel_url)
                    else _tr("未下载 · 启用后自动部署")
                )
            elif plugin.id == "easytier":
                widgets["info"].set(_tr("正在读取对端…"))
            elif plugin.id == "muse2api" and deployed:
                import gateways

                chrome = _tr("Chrome 已找到") if gateways.chrome_path().is_file() else _tr("缺 Chrome")
                widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                widgets["info"].set(str(plugins.plugin_root(plugin.id)) if deployed else _tr("未下载 · 启用后自动部署"))

    def plugin_actions(self, plugin_id):
        import services

        if plugin_id == "grok-bot":
            import grok_bot_cli
            return ((_tr("安装/修复"), grok_bot_cli.install), (_tr("检查连接"), grok_bot_cli.check),
                    (_tr("使用说明"), grok_bot_cli.guide))

        def log(path_or_fn):
            def open_log():
                target = Path(path_or_fn() if callable(path_or_fn) else path_or_fn)
                if not target.exists():
                    return _tr('日志不存在：{0}').format(f'{target}')
                os.startfile(target)
                return _tr('已打开日志 {0}').format(f'{target.name}')
            return open_log
        start = lambda: services.plugin_start(plugin_id)
        stop = lambda: services.plugin_stop(plugin_id)
        if plugin_id == "dot2api":
            import dot_reconnect

            return ((_tr("启动"), start), (_tr("停止"), stop), (_tr("重连"), lambda: dot_reconnect.reconnect() or _tr("隧道地址未变化，无需重连")),
                    (_tr("日志"), log(lambda: services.LOG_DIR / "serve.log")))
        if plugin_id == "muse2api":
            import gateways

            console = f"{gateways.MUSE_URL}/dashboard"
            return ((_tr("管理台"), lambda: gateways.open_console(console)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("日志"), log(lambda: gateways.MUSE_LOG)))
        if plugin_id == "easytier":
            import easytier
            import network_status

            return ((_tr("局域网"), lambda: network_status.open_window() or _tr("已打开局域网状态")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("重启"), easytier.restart))
        if plugin_id == "chatgpt-web":
            return ((_tr("打开"), lambda: services.start_chatgpt_web(True)), (_tr("启动"), start), (_tr("停止"), stop),
                    (_tr("重启"), lambda: f"{services.stop_chatgpt_web()}；{services.start_chatgpt_web(False)}"),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "chatgpt-web.log")))
        if plugin_id == "gemini-web":
            return ((_tr("打开"), lambda: services.start_gemini_web(True)), (_tr("停止"), stop),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "gemini-web.log")))
        if plugin_id == "usage-float":
            return ((_tr("悬浮窗"), lambda: services.component_action("usage-float", "show")), (_tr("启动"), start),
                    (_tr("停止"), stop), (_tr("刷新"), lambda: services.component_action("usage-float", "refresh")),
                    (_tr("设置"), lambda: services.component_action("usage-float", "settings")),
                    (_tr("日志"), log(lambda: services.STATE_DIR / "usage-float.log")))
        return ()

    def plugin_act(self, plugin_id, work):
        if plugin_id == 'grok-bot':
            import grok_bot_cli
            if work is grok_bot_cli.check:
                self.check_grok_bot()
                return
        self.run(work, self.say(self.load_plugins), _tr("正在执行…"), long=plugin_id == "grok-bot")

    def check_grok_bot(self):
        if self.closing or self.current != 'plugins' or getattr(self, '_grok_probe_busy', False):
            return
        import grok_bot_cli
        self._grok_probe_busy = True

        def work():
            try:
                return grok_bot_cli.check()
            finally:
                self._grok_probe_busy = False

        def done(message):
            if not self.closing and self.current == 'plugins':
                self.status.set(message)
                self._keep_status_once = message
                try:
                    self.load_plugins(probe=False)
                finally:
                    self._keep_status_once = None
        # Let an in-flight bounded read finish on close, as with existing long actions.
        self.run(work, done, _tr('正在检查 Grok Bot 连接…'), long=True)

    def _record_plugin_flag(self, plugin_id: str, key: str, value: bool) -> None:
        flags = getattr(self, "_user_plugin_flags", None)
        if flags is None:
            flags = {}
            self._user_plugin_flags = flags
        flags[(plugin_id, key)] = (getattr(self, "_plugins_load_generation", 0), bool(value))

    def _persist_plugin_flag(self, plugin_id: str, key: str, value: bool) -> bool:
        value = bool(value)
        try:
            plugins.set_flag(plugin_id, key, value)
        except Exception as exc:
            disk_val = bool(plugins.load().get(plugin_id, {}).get(key, not value))
            widgets = getattr(self, "plugin_widgets", {}).get(plugin_id)
            if widgets and key in widgets:
                widgets[key].set(disk_val)
            flags = getattr(self, "_user_plugin_flags", None)
            if flags is not None:
                flags.pop((plugin_id, key), None)
            self.status.set(_tr('操作失败：{0}').format(f'{exc}'))
            return False
        self._record_plugin_flag(plugin_id, key, value)
        return True

    def toggle_enabled(self, plugin_id, value):
        import services

        value = bool(value)
        if not self._persist_plugin_flag(plugin_id, "enabled", value):
            return
        self._cards_cache = None
        self._plugins_update_result = None

        def work():
            name = plugins.BY_ID[plugin_id].name
            current = plugins.enabled(plugin_id)
            if current != value:
                return _tr('已启用 {0}').format(f'{name}') if current else _tr('已停用 {0}').format(f'{name}')
            if not value:
                try:
                    return _tr('已停用 {0}；{1}').format(f'{name}', f'{services.plugin_stop(plugin_id)}')
                except Exception as exc:
                    return _tr('已停用 {0}（停止时出错：{1}）').format(f'{name}', f'{exc}')
            if plugins.autostarts(plugin_id):
                try:
                    return _tr('已启用 {0}；{1}').format(f'{name}', f'{services.plugin_start(plugin_id)}')
                except Exception as exc:
                    return _tr('已启用 {0}（启动时出错：{1}）').format(f'{name}', f'{exc}')
            spec = plugins.BY_ID[plugin_id]
            root = plugins.plugin_root(plugin_id)
            if spec.repo and not (root / ".git").exists():
                import updates

                try:
                    deployed = updates.ensure_plugin(plugin_id)
                    services.refresh_plugin_roots()
                    return _tr('已启用 {0}；{1}').format(f'{name}', f'{deployed}')
                except Exception as exc:
                    return _tr('已启用 {0}（部署时出错：{1}）').format(f'{name}', f'{exc}')
            return _tr('已启用 {0}').format(f'{name}')
        if value and plugin_id == "muse2api":
            # First enable downloads and installs its dependencies: show it in the update window.
            import update_status
            import updates

            self._open_update_window_if_interactive()

            def deploy_work():
                name = plugins.BY_ID[plugin_id].name
                if not plugins.enabled(plugin_id):
                    return _tr('已停用 {0}').format(f'{name}')
                try:
                    return _tr('已启用 {0}；').format(f'{name}') + updates.deploy_gateway(
                        plugin_id, plugins.autostarts(plugin_id), progress=update_status.record
                    )
                except Exception as exc:
                    panel_widgets.log_error(exc)
                    return _tr('已启用 {0}（部署时出错：{1}）').format(f'{name}', f'{exc}')

            self.run(deploy_work, self.say(self.load_plugins), _tr("正在下载并构建…（关闭窗口不会中断）"), long=True)
            return
        self.run(work, self.say(self.load_plugins))

    def toggle_autostart(self, plugin_id, value):
        value = bool(value)
        if not self._persist_plugin_flag(plugin_id, "autostart", value):
            return
        self._plugins_update_result = None
        name = plugins.BY_ID[plugin_id].name
        state_text = _tr('开') if value else _tr('关')
        self.run(lambda: _tr('{0} 随托盘自启：{1}').format(f'{name}', f'{state_text}'))

    def load_plugins(self, probe=True):
        import services

        keep_status = getattr(self, "_keep_status_once", None)
        update_result = getattr(self, "_plugins_update_result", None)
        if not keep_status or (update_result and keep_status != update_result[0]):
            self._plugins_update_result = None
            update_result = None
        if probe and not keep_status:
            self._grok_auto_check_pending = True
        self._plugins_load_generation = getattr(self, '_plugins_load_generation', 0) + 1
        generation = self._plugins_load_generation

        def work():
            import easytier

            def label(plugin_id):
                try:
                    if plugin_id == "grok-bot":
                        import grok_bot_cli
                        reading = grok_bot_cli.snapshot()
                        return reading['label'], reading
                    if plugin_id == "easytier":
                        reading = easytier.snapshot()
                        peers_raw = reading.get("peers")
                        peer_count = (
                            None
                            if reading.get("state") == "running" and peers_raw is None
                            else len(easytier.remote_peers(peers_raw))
                        )
                        return easytier.label(reading), peer_count
                    return services.plugin_label(plugin_id), None
                except Exception as exc:
                    return _tr('状态异常：{0}').format(f'{exc}')[:30], None
            found = {}
            threads = []
            for pid in plugins.BY_ID:
                t = threading.Thread(target=lambda p=pid: found.__setitem__(p, label(p)), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            config = plugins.load()
            labels = {plugin_id: value[0] for plugin_id, value in found.items()}
            data = self.data_module()
            extras = {"dot2api": data.dot_details(), "easytier": found["easytier"][1],
                      "grok-bot": found["grok-bot"][1]}
            extras["dot2api"]["subscription"] = data.subscription_text()
            return config, labels, extras
        def done(value):
            if generation == self._plugins_load_generation:
                config, labels, extras = value
                for (pid, key), (rec_gen, flag_val) in (getattr(self, "_user_plugin_flags", None) or {}).items():
                    if rec_gen >= generation and pid in config and isinstance(config[pid], dict):
                        config[pid][key] = flag_val
                self.plugins_loaded(
                    (config, labels, extras),
                    keep_status=keep_status,
                    update_result=update_result,
                )
        self.run(work, done, None if keep_status else _tr("正在读取集成状态…"))

    def plugins_loaded(self, value, keep_status=None, update_result=None):
        config, labels, extras = value
        enabled_count = 0
        running_count = 0
        expected_running_count = 0
        expected_ok_count = 0
        attention_count = 0
        for plugin in plugins.PLUGINS:
            widgets = self.plugin_widgets[plugin.id]
            row = config[plugin.id]
            if plugin.id == "grok-bot":
                import grok_bot_cli
                reading = extras.get(plugin.id) or {}
                kind = reading.get('kind', 'idle')
                if reading.get('installed'):
                    enabled_count += 1
                    if kind == 'ok':
                        running_count += 1
                    elif kind in ('warn', 'error', 'danger'):
                        attention_count += 1
                theme.set_pill(widgets["status"], labels[plugin.id], kind)
                widgets["info"].set(grok_bot_cli.info())
                if getattr(self, '_grok_auto_check_pending', False):
                    self._grok_auto_check_pending = False
                    if reading.get('installed'):
                        self.check_grok_bot()
                continue
            deployed = not plugin.repo or (plugins.plugin_root(plugin.id) / ".git").exists()
            raw_label = labels[plugin.id]
            if not deployed and raw_label == _tr("已停止"):
                raw_label = _tr("未安装")
            raw_kind = theme.status_kind(raw_label)
            if row["enabled"]:
                enabled_count += 1
                if raw_kind == "ok":
                    running_count += 1
                elif raw_kind in ("warn", "error", "danger"):
                    attention_count += 1
                if row["autostart"]:
                    expected_running_count += 1
                    if raw_kind == "ok":
                        expected_ok_count += 1
            label = raw_label if row["enabled"] else _tr('未启用 · {0}').format(f'{raw_label}')
            pill_label = label if len(label) <= 42 else f"{label[:41]}…"
            theme.set_pill(widgets["status"], pill_label, raw_kind if row["enabled"] else "idle")
            widgets["enabled"].set(row["enabled"])
            widgets["autostart"].set(row["autostart"])
            if plugin.id == "dot2api":
                dot = extras["dot2api"]
                self.tunnel_url = dot.get("tunnel_url") or ""
                if deployed or self.tunnel_url or raw_kind == "ok":
                    widgets["info"].set(f"{self.tunnel_url or _tr('隧道未发布')} · {dot.get('subscription')}")
                else:
                    widgets["info"].set(_tr("未下载 · 启用后自动部署"))
            elif plugin.id == "easytier":
                peers = extras.get("easytier")
                widgets["info"].set(_tr("对端读取失败") if peers is None else _tr('可见对端 {0} 个').format(f'{peers}'))
            elif plugin.id == "muse2api" and deployed:
                import gateways

                chrome = _tr("Chrome 已找到") if gateways.chrome_path().is_file() else _tr("缺 Chrome")
                widgets["info"].set(_tr('{0}/v1 · {1} · muse.ai cookie 在管理台导入').format(f'{gateways.MUSE_URL}', f'{chrome}'))
            else:
                widgets["info"].set(str(plugins.plugin_root(plugin.id)) if deployed else _tr("未下载 · 启用后自动部署"))
        if not getattr(self, "plugins_updating", False):
            if update_result:
                summary_text, pill = update_result
                if hasattr(self, "plugins_status") and summary_text:
                    self.plugins_status.set(summary_text)
                if hasattr(self, "plugins_pill") and pill:
                    theme.set_pill(self.plugins_pill, *pill)
            else:
                if hasattr(self, "plugins_status"):
                    self.plugins_status.set(_tr("集成：{0} 已启用 · {1} 运行中").format(enabled_count, running_count))
                if hasattr(self, "plugins_pill"):
                    if attention_count > 0:
                        theme.set_pill(self.plugins_pill, _tr("部分异常"), "warn")
                    else:
                        all_ok = enabled_count > 0 and (
                            (expected_running_count > 0 and expected_ok_count >= expected_running_count)
                            or running_count >= enabled_count
                        )
                        theme.set_pill(self.plugins_pill, _tr("正常") if all_ok else _tr("就绪"), "ok" if all_ok else "idle")
            self.status.set(keep_status or _tr('集成状态已更新 · {0}').format(f"{time.strftime('%H:%M:%S')}"))

    def copy_tunnel(self):
        url = getattr(self, "tunnel_url", "")
        if not url:
            self.status.set(_tr("还没有隧道 URL"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.status.set(_tr('已复制 {0}').format(f'{url}'))

    def copy_secret(self, value, label):
        if not value:
            self.status.set(_tr('还没有{0}').format(f'{label}'))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.set(_tr('已复制{0}').format(f'{label}'))

    def import_account(self, plugin_id):
        """Sign in to muse.ai in a dedicated Chrome; the cookie is then imported through the gateway's API."""
        import account_login
        import services

        def work():
            ready = services.muse_ready
            if not ready():
                services.plugin_start(plugin_id)
                deadline = time.monotonic() + 60
                while not ready():
                    if time.monotonic() >= deadline:
                        return _tr('{0} 没有启动，未导入，详见日志').format(plugins.BY_ID[plugin_id].name)
                    time.sleep(1)
            return account_login.login_and_import(plugin_id)

        self.run(work, self.say(self.load_plugins), _tr("已打开登录窗口：在里面登录，完成后自动导入（10 分钟内有效）…"), long=True)

    def copy_muse_key(self):
        import gateways

        self.copy_secret(gateways.muse_api_key(), _tr(" Muse2API API Key（首次启动后才生成）"))

    def restart_services(self):
        import services

        self.run(services.restart_services, self.say(self.load_plugins), _tr("正在重启已启用服务…"))

    def update_components(self):
        import update_status
        import updates

        if getattr(self, "plugins_updating", False):
            return
        self.plugins_updating = True
        self._plugins_update_result = None
        if hasattr(self, "plugins_update_button"):
            self.plugins_update_button.state(["disabled"])
            self.plugins_update_button.configure(text=_tr("更新中…"))
        if hasattr(self, "plugins_pill"):
            theme.set_pill(self.plugins_pill, _tr("更新中"), "warn")
        if hasattr(self, "plugins_status"):
            self.plugins_status.set(_tr("正在检查并更新组件…（关闭窗口不会中断）"))
        self._open_update_window_if_interactive()
        generation = self._generation

        def progress(event):
            update_status.record(event)
            comp = event.get("component", "") if isinstance(event, dict) else ""
            msg = event.get("message", "") if isinstance(event, dict) else str(event)
            text = f"{comp} · {msg}" if comp and msg and not msg.startswith(comp) else (msg or comp)
            if text:
                self._post_ui_progress(generation, self.plugins_progress, text)

        def work():
            try:
                return updates.update_all(progress=progress)
            except Exception as error:
                panel_widgets.log_error(error)
                return updates.UpdateAllResult(
                    _tr("检查更新失败：{0}").format(error),
                    components={"batch": {"status": "failed", "message": str(error)}},
                )

        def done(result):
            self.plugins_updating = False
            if hasattr(self, "plugins_update_button"):
                self.plugins_update_button.state(["!disabled"])
                self.plugins_update_button.configure(text=_tr("检查更新"))
            structured = isinstance(result, dict) or hasattr(result, "statuses")
            text = str(result.get("summary") if isinstance(result, dict) else (result or ""))
            busy = bool(result.get("busy")) if structured else (text == _tr("正在更新，请稍候"))
            statuses = dict(result.get("statuses") or {}) if structured else {}
            if structured and not statuses and isinstance(result.get("components"), dict):
                statuses = {
                    k: (v.get("status", "failed") if isinstance(v, dict) else str(v))
                    for k, v in result["components"].items()
                }
            failed = bool(
                (result.get("failed") or result.get("deferred")) if structured else False
            ) or any(st in ("failed", "deferred") for st in statuses.values())
            product_status = str(result.get("product_status") or "dev") if structured else "dev"
            product_warn = product_status in ("newer", "failed")
            active_statuses = [st for st in statuses.values() if st != "skipped"]
            all_up_to_date = (
                structured
                and not busy
                and not failed
                and not product_warn
                and product_status in ("current", "ahead", "dev")
                and bool(active_statuses)
                and all(st in ("current", "updated") for st in active_statuses)
            )
            if busy:
                pill = (_tr("正在更新"), "warn")
            elif failed:
                pill = (_tr("部分异常"), "warn")
            elif product_warn:
                pill = (_tr("需注意"), "warn")
            elif all_up_to_date:
                pill = (_tr("已是最新"), "ok")
            else:
                pill = (_tr("就绪"), "idle")
            if hasattr(self, "plugins_pill"):
                theme.set_pill(self.plugins_pill, *pill)
            if hasattr(self, "plugins_status") and text:
                self.plugins_status.set(text)
            if text:
                self.status.set(text)
            self._plugins_update_result = (text, pill) if text else None
            self._keep_status_once = text or None
            try:
                self.load_plugins()
            finally:
                self._keep_status_once = None

        self.run(work, done,
                 _tr("正在检查并更新组件…（关闭窗口不会中断）"), long=True)

    def plugins_progress(self, message):
        if hasattr(self, "plugins_status"):
            self.plugins_status.set(message)
        self.status.set(message)

    def open_updates(self):
        import update_status

        update_status.open_window()
