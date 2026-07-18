"""予約ページ(customtkinter版)。編集ダイアログは既存の SchedDialog を再利用する。"""
from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from .app_ctk import Page, ask, fill, picked, tree
from .widgets import MUTED

COLS = ("kind", "action", "days", "times", "keep", "enabled")
H = {"kind": ("種別", 120), "action": ("動作", 200), "days": ("曜日", 80),
     "times": ("時刻", 160), "keep": ("世代", 70), "enabled": ("有効", 90)}
KINDS = {"ark-all": "🦖 ARK全", "ark-players": "🧬 プレイヤー", "ark": "🦖 ARK",
         "mc": "🟩/🐑"}


class SchedPage(Page):
    def build(self):
        self._rows = []
        self._targets = []
        self.title("⏰ 予約")
        self.t = tree(self, COLS, H, "対象", 240, 9)
        self.t.bind("<Double-1>", lambda _e: self._edit())
        b = self.bar()
        self.btn(b, "＋ 追加", lambda: self._edit(new=True), "primary").pack(side="left",
                                                                          padx=(0, 6))
        self.btn(b, "✎ 編集", self._edit).pack(side="left", padx=(0, 6))
        self.btn(b, "⏯ 有効/無効", self._toggle).pack(side="left", padx=(0, 6))
        self.btn(b, "🗑 削除", self._delete, "danger").pack(side="left", padx=(0, 6))
        self.btn(b, "▶ 今すぐ実行", self._run).pack(side="left")
        ctk.CTkLabel(self, text="予約は常駐サービスが実行します(この画面を閉じても動きます)",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(8, 0))
        self._load_targets()
        self.poll(self.client.schedules, self._fill, ms=6000)

    def _load_targets(self):
        def done(data, err):
            if err:
                return
            ark, servers = data
            t = [("🦖 ARK 全マップ(停止中は自動スキップ)", "ark-all", "*", "ARK 全マップ"),
                 ("🧬 ARK プレイヤーデータのみ(全マップ+クラスタ・軽量)", "ark-players",
                  "*", "ARK プレイヤーデータ")]
            t += [(f"🦖 {a['display_name']}", "ark", a["map_label"], a["display_name"])
                  for a in ark]
            t += [(("🐑 " if s["game"] == "palworld" else "🟩 ") + s["display_name"],
                   "mc", s["name"], s["display_name"]) for s in servers]
            self._targets = t
        self.worker.submit(lambda: (self.client.ark(), self.client.servers()), done)

    def _fill(self, rows):
        self._rows = rows
        out = []
        for j in rows:
            nxt = j.get("next_interval_in_sec")
            times = j["times_text"] + (f"  (次 {nxt / 60:.0f}分)" if nxt else "")
            out.append((j["id"], j["display"],
                        (KINDS.get(j["kind"], j["kind"]), j["action_text"],
                         j["days_text"], times, j["keep"] or "既定",
                         "✅ 有効" if j["enabled"] else "⏸ 無効"),
                        ("active" if j["enabled"] else "off",)))
        fill(self.t, out)

    def _sel(self):
        return picked(self, self.t, self._rows, "id", "予約")

    def _save(self, rows):
        def done(_r, err):
            if err:
                messagebox.showerror("予約の保存", str(err), parent=self)
            else:
                self.worker.submit(self.client.schedules,
                                   lambda r, e: self._fill(r) if e is None else None)
        self.worker.submit(lambda: self.client.save_schedules(rows), done)

    def _toggle(self):
        j = self._sel()
        if j:
            self._save([dict(r, enabled=(not r["enabled"]) if r["id"] == j["id"]
                             else r["enabled"]) for r in self._rows])

    def _delete(self):
        j = self._sel()
        if j and ask(self, f"予約「{j['display']}」を削除しますか?"):
            self._save([r for r in self._rows if r["id"] != j["id"]])

    def _run(self):
        j = self._sel()
        if j and ask(self, f"「{j['display']}」の{j['action_text']}を今すぐ実行しますか?"):
            self.act(lambda: self.client.run_schedule(j["id"]), f"実行 {j['display']}")

    def _edit(self, new: bool = False):
        job = None if new else self._sel()
        if not new and job is None:
            return
        if not self._targets:
            messagebox.showinfo("準備中", "対象一覧を取得中です。少し待ってください。",
                                parent=self)
            return
        from .sched_app import SchedDialog
        SchedDialog(self.winfo_toplevel(), job, self._targets, self._ok)

    def _ok(self, job: dict):
        self._save([r for r in self._rows if r["id"] != job["id"]] + [job])
