"""A read-only EasyTier network window, isolated from the tray event loop."""
from __future__ import annotations

from i18n import translate as _tr

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import component_launch
import easytier

_process = None


def open_window() -> None:
    global _process
    if _process is not None and _process.poll() is None:
        import win32con
        import win32gui
        import win32process

        def show(hwnd, _):
            if win32process.GetWindowThreadProcessId(hwnd)[1] == _process.pid and win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    win32gui.FlashWindow(hwnd, True)
        win32gui.EnumWindows(show, None)
        return
    _process = subprocess.Popen(component_launch.command(__file__), creationflags=easytier.CREATE_NO_WINDOW)


def peer_values(peer: dict) -> tuple:
    local = peer.get("cost") == "Local"
    cost = peer.get("cost", "")
    link = _tr("本机") if local else _tr("直连") if cost == "p2p" else _tr('中转（{0}）').format(f'{cost}') if cost else _tr("未知")
    return (peer.get("hostname") or _tr("未命名节点"), peer.get("ipv4") or _tr("未分配"),
            link, "—" if local else f"{peer.get('lat_ms', '—')} ms",
            peer.get("loss_rate", "—"), peer.get("tunnel_proto", "—"),
            peer.get("rx_bytes", "—"), peer.get("tx_bytes", "—"))


class NetworkWindow:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.results = queue.Queue()
        self.busy = False
        self.next_refresh = 0.0
        self.addresses = {}
        import ui_theme as theme

        theme.apply(root, _tr("局域网状态 · Route Manager"), "1080x520", (820, 390))
        ttk.Style(root).configure("Treeview", rowheight=theme.px(36))
        body = ttk.Frame(root, padding=24)
        body.pack(fill="both", expand=True)
        header = ttk.Frame(body)
        header.pack(fill="x")
        ttk.Label(header, text=_tr("局域网状态"), style="Title.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(header, text=_tr("立即刷新"), style="Accent.TButton", command=self.refresh)
        self.refresh_button.pack(side="right")
        ttk.Label(body, text=_tr("EasyTier 虚拟局域网 · 显示本机可见节点 · 延迟为本机到各节点的测量值"),
                  style="Sub.TLabel").pack(anchor="w", pady=(8, 18))
        self.summary = tk.StringVar(value=_tr("正在读取网络状态…"))
        ttk.Label(body, textvariable=self.summary, style="H2.TLabel").pack(anchor="w", pady=(0, 14))

        table_frame = theme.card(body, padding=1, fill="both", expand=True)
        columns = ("host", "ip", "link", "latency", "loss", "protocol", "rx", "tx")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=3)
        for key, title, width in zip(columns, (_tr("机器名称"), _tr("虚拟 IP"), _tr("连接方式"), _tr("延迟"), _tr("丢包"), _tr("协议"), _tr("累计接收"), _tr("累计发送")),
                                     (225, 145, 100, 90, 75, 85, 110, 110)):
            self.table.heading(key, text=title)
            self.table.column(key, width=theme.px(width), minwidth=theme.px(65), anchor="w")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.table.tag_configure("local", background=theme.ACCENT_SOFT)
        self.table.bind("<Double-1>", lambda event: self.copy_ip())
        self.table.bind("<Control-c>", lambda event: self.copy_ip())

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(14, 0))
        self.status = tk.StringVar(value=_tr("每 10 秒自动刷新；未出现在列表中的机器不代表已离线。"))
        ttk.Label(footer, textvariable=self.status, style="Sub.TLabel").pack(side="left")
        ttk.Button(footer, text=_tr("复制所选 IP"), command=self.copy_ip).pack(side="right")
        self.refresh()
        root.after(100, self.poll)

    def copy_ip(self):
        selected = self.table.selection()
        address = self.addresses.get(selected[0]) if selected else None
        if not address:
            self.status.set(_tr("请先选择有虚拟 IP 的机器。"))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(address)
        self.status.set(_tr('已复制 {0}').format(f'{address}'))

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.refresh_button.configure(state="disabled", text=_tr("刷新中…"))

        def read():
            try:
                self.results.put((easytier.snapshot(), None))
            except Exception as exc:
                self.results.put((None, str(exc)))
        threading.Thread(target=read, daemon=True).start()

    def render(self, reading, error):
        self.busy = False
        self.next_refresh = time.monotonic() + 10
        self.refresh_button.configure(state="normal", text=_tr("立即刷新"))
        if error:
            self.summary.set(_tr("读取失败 · 列表为上次成功读取的结果"))
            self.status.set(_tr("暂时无法读取网络状态，请稍后刷新。"))
            return
        selected = self.table.selection()
        for row in self.table.get_children():
            self.table.delete(row)
        self.addresses.clear()
        peers = sorted(reading.get("peers") or [], key=lambda p: (p.get("cost") != "Local", p.get("hostname", "")))
        for index, peer in enumerate(peers):
            key = str(peer.get("id") or index)
            self.addresses[key] = peer.get("ipv4")
            self.table.insert("", "end", iid=key, values=peer_values(peer),
                              tags=("local",) if peer.get("cost") == "Local" else ())
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])
        if reading["state"] == easytier.RUNNING:
            direct = sum(p.get("cost") == "p2p" for p in peers)
            self.summary.set(_tr('网络运行中  ·  {0} 个可见节点  ·  {1} 个直连对端').format(f'{len(peers)}', f'{direct}'))
            self.status.set(_tr('更新于 {0} · 每 10 秒刷新 · 双击机器可复制 IP').format(f"{time.strftime('%H:%M:%S')}"))
        else:
            self.summary.set(f"EasyTier {easytier.label(reading)}")
            self.status.set(_tr("暂时无法获取节点列表；可在托盘 EasyTier 菜单查看服务状态。"))

    def poll(self):
        try:
            reading, error = self.results.get_nowait()
            self.render(reading, error)
        except queue.Empty:
            pass
        if not self.busy and time.monotonic() >= self.next_refresh:
            self.refresh()
        self.root.after(100, self.poll)


def main():
    import ctypes
    import tkinter as tk

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    NetworkWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
