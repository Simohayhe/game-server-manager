"""ARK専用GUI。共通土台(BaseApp)の上に「ARKの画面」だけを書く。

旧 gui/app.py の「🦖 ARK」タブ相当だが:
  - 自分でPowerShell/RCONを叩かない → 起動が速く、状態はサービスのキャッシュから即表示。
  - この画面を閉じても、予約・バックアップ・動的設定配信は止まらない(サービスが持つ)。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .common import (DEFAULT_BASE, BaseApp, RconConsole, confirm, fill_tree, hint,
                     make_tree, selected_row)

COLS = ("status", "players", "uptime", "build")
HEADS = {"status": ("状態", 90), "players": ("人数", 60),
         "uptime": ("稼働時間", 110), "build": ("ビルド", 110)}


class ArkApp(BaseApp):
    def __init__(self, base: str):
        self._rows: list[dict] = []
        super().__init__(base, "GSM — ARK サーバー管理", size="1000x680")
        self.poll(self.client.ark, self._fill)

    def build_tabs(self) -> None:
        f = self.add_tab("🦖 サーバー")
        hint(f, "↳ 行を選んで操作。状態は常駐サービスが監視しているので即表示されます。")
        self.tree = make_tree(f, COLS, HEADS, first_head="サーバー", first_width=280,
                              height=12)

        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=8, pady=6)
        for text, cmd in (("▶ 起動", self._start), ("■ 停止", self._stop),
                          ("🔁 再起動", self._restart), ("💾 バックアップ", self._backup),
                          ("⬆ 更新", self._update)):
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=3)
        ttk.Button(bar, text="🧬 プレイヤーデータBK",
                   command=self._players_backup).pack(side=tk.LEFT, padx=3)

        self.console = RconConsole(f, self.worker, self._rcon_fn)
        self.console.pack(fill=tk.BOTH, expand=True)

    # ---------------- 表示 ----------------
    def _fill(self, rows: list[dict]) -> None:
        self._rows = rows
        out = []
        for a in rows:
            running = a.get("running")
            st = "🟢 稼働中" if running else ("⚪ 停止中" if running is not None else "…")
            name = a["display_name"] + (f"  (:{a['game_port']})" if a.get("game_port") else "")
            out.append((str(a["index"]), name,
                        (st, a.get("player_count") if running else "-",
                         a.get("uptime_text", "―"), a.get("build") or "―"),
                        ("active" if running else "off",)))
        fill_tree(self.tree, out)

    def _selected(self) -> dict | None:
        return selected_row(self, self.tree, self._rows, "index", "対象のマップ")

    # ---------------- 操作 ----------------
    def _start(self) -> None:
        a = self._selected()
        if a:
            self.run_action(lambda: self.client.ark_start(a["index"]),
                            f"起動 {a['display_name']}", self.console.log)

    def _stop(self) -> None:
        a = self._selected()
        if a and confirm(self, "確認", f"{a['display_name']} を停止しますか?\n"
                                       "(プレイヤーが居れば60/30/10秒前に予告します)"):
            self.run_action(lambda: self.client.ark_stop(a["index"]),
                            f"停止 {a['display_name']}", self.console.log)

    def _restart(self) -> None:
        a = self._selected()
        if a and confirm(self, "確認", f"{a['display_name']} を再起動しますか?\n"
                                       "(プレイヤーが居れば60/30/10秒前に予告します)"):
            self.run_action(lambda: self.client.ark_restart(a["index"]),
                            f"再起動 {a['display_name']}", self.console.log)

    def _backup(self) -> None:
        a = self._selected()
        if a:
            self.run_action(lambda: self.client.ark_backup(a["index"]),
                            f"バックアップ {a['display_name']}", self.console.log)

    def _update(self) -> None:
        a = self._selected()
        if a and messagebox.askyesno(
                "確認",
                f"{a['display_name']} を更新しますか?\n\n"
                "更新がある場合のみ、停止(予告あり)→更新→元が稼働中なら起動します。\n"
                "更新が無ければ何もしません。", parent=self):
            self.run_action(lambda: self.client.ark_update(a["index"]),
                            f"更新 {a['display_name']}", self.console.log)

    def _players_backup(self) -> None:
        self.run_action(self.client.ark_players_backup, "プレイヤーデータBK",
                        self.console.log)

    def _rcon_fn(self, cmd: str):
        a = self._selected()
        if not a:
            return None                      # 未選択 → 送信しない
        return lambda: self.client.ark_rcon(a["index"], cmd)


def run(base: str = DEFAULT_BASE) -> None:
    ArkApp(base).mainloop()
