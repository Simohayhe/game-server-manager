"""Minecraft / Palworld 用GUI(VM上で動くサーバー)。共通土台(BaseApp)の上に構築。

MCとPalworldは「VM上のsystemdサービス + RCON」で操作がほぼ同じなので1画面にまとめる。
(ARKはホストのプロセス直操作で系統が違うため別exe)
    python main_mc.py                     → マイクラ
    python main_mc.py --game all          → VM上の全部
    python main_pal.py                    → Palworld
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .common import (DEFAULT_BASE, BaseApp, RconConsole, confirm, fill_tree, hint,
                     make_tree, selected_row)

GAME_LABEL = {"minecraft": "🟩 Minecraft", "palworld": "🐑 Palworld", "ark": "🦖 ARK"}
SRV_COLS = ("game", "status", "vm", "address")
SRV_HEADS = {"game": ("種別", 110), "status": ("状態", 90), "vm": ("VM", 110),
             "address": ("アドレス", 210)}
VM_COLS = ("state", "mem", "servers")
VM_HEADS = {"state": ("状態", 90), "mem": ("メモリ", 90), "servers": ("上で動くサーバー", 360)}


class ServerApp(BaseApp):
    def __init__(self, base: str, game: str | None = None, title: str | None = None):
        self.game = game
        self._rows: list[dict] = []
        self._vms: list[dict] = []
        super().__init__(base, title or "GSM — サーバー管理", size="1020x700")
        self.poll(lambda: (self.client.servers(), self.client.vms()), self._on_data)

    def build_tabs(self) -> None:
        self._build_servers(self.add_tab("🎮 サーバー"))
        self._build_vms(self.add_tab("🖥 VM"))

    def _build_servers(self, f) -> None:
        hint(f, "↳ 行を選んで操作。状態は常駐サービスが監視しています。")
        self.tree = make_tree(f, SRV_COLS, SRV_HEADS, first_head="サーバー",
                              first_width=240, height=10)
        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=8, pady=6)
        for text, act in (("▶ 起動", "start"), ("■ 停止", "stop"), ("🔁 再起動", "restart")):
            ttk.Button(bar, text=text,
                       command=lambda a=act: self._action(a)).pack(side=tk.LEFT, padx=3)
        self.console = RconConsole(f, self.worker, self._rcon_fn)
        self.console.pack(fill=tk.BOTH, expand=True)

    def _build_vms(self, f) -> None:
        hint(f, "↳ VM停止時は、上で動くゲームサーバーを先に安全停止(ワールド保存)します。")
        self.vmtree = make_tree(f, VM_COLS, VM_HEADS, first_head="VM",
                                first_width=180, height=10)
        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(bar, text="▶ VM起動", command=self._vm_start).pack(side=tk.LEFT, padx=3)
        ttk.Button(bar, text="■ VM停止(安全)",
                   command=lambda: self._vm_stop(False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(bar, text="⏹ VM強制停止",
                   command=lambda: self._vm_stop(True)).pack(side=tk.LEFT, padx=3)

    # ---------------- 表示 ----------------
    def _on_data(self, data) -> None:
        servers, vms = data
        self._rows = [s for s in servers if self.game is None or s["game"] == self.game]
        self._vms = vms
        marks = {"active": "🟢 稼働中", "inactive": "⚪ 停止中", "error": "⚠ エラー"}
        fill_tree(self.tree, [
            (s["name"], s["display_name"],
             (GAME_LABEL.get(s["game"], s["game"]),
              marks.get(s.get("status"), str(s.get("status") or "…")),
              s.get("vm") or "-", s.get("fqdn") or s.get("address") or ""),
             ("active" if s.get("status") == "active"
              else "err" if s.get("status") == "error" else "off",))
            for s in self._rows])
        fill_tree(self.vmtree, [
            (v["name"], v["name"],
             ("🟢 Running" if v["state"] == "Running" else f"⚪ {v['state']}",
              f"{v['memory_mb']:,} MB" if v["memory_mb"] else "-",
              ", ".join(v.get("servers") or []) or "-"),
             ("active" if v["state"] == "Running" else "off",))
            for v in vms])

    def _selected(self) -> dict | None:
        return selected_row(self, self.tree, self._rows, "name", "サーバー")

    # ---------------- 操作 ----------------
    def _action(self, act: str) -> None:
        s = self._selected()
        if not s:
            return
        jp = {"start": "起動", "stop": "停止", "restart": "再起動"}[act]
        if act in ("stop", "restart") and not confirm(
                self, "確認", f"{s['display_name']} を{jp}しますか?"):
            return
        self.run_action(lambda: self.client.server_action(s["name"], act),
                        f"{jp} {s['display_name']}", self.console.log)

    def _rcon_fn(self, cmd: str):
        s = self._selected()
        if not s:
            return None
        return lambda: self.client.server_rcon(s["name"], cmd)

    def _selected_vm(self) -> dict | None:
        return selected_row(self, self.vmtree, self._vms, "name", "VM")

    def _vm_start(self) -> None:
        v = self._selected_vm()
        if v:
            name = v["name"]
            self.run_action(lambda: self.client.vm_start(name), f"VM起動 {name}",
                            self.console.log)

    def _vm_stop(self, force: bool) -> None:
        v = self._selected_vm()
        if not v:
            return
        name = v["name"]
        on = ", ".join(v.get("servers") or []) or "なし"
        if not confirm(self, "確認",
                       f"VM {name} を{'強制' if force else ''}停止しますか?\n\n"
                       f"このVM上のサーバー: {on}\n"
                       "先にゲームサーバーを安全停止(ワールド保存)してからVMを止めます。"):
            return
        self.run_action(lambda: self.client.vm_stop(name, force=force),
                        f"VM{'強制' if force else ''}停止 {name}", self.console.log)


def run(base: str = DEFAULT_BASE, game: str | None = None,
        title: str | None = None) -> None:
    ServerApp(base, game=game, title=title).mainloop()
