"""予約(スケジューラ)のビュー。シェルにも単体exeにも埋め込める Frame。

編集ダイアログは gui/sched_app.py の SchedDialog を再利用する(実装を二重に持たない)。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .theme import ToolBar
from .views import BaseView, confirm, fill_tree, make_tree, selected_row, show_error

COLS = ("kind", "action", "days", "times", "keep", "enabled")
HEADS = {"kind": ("種別", 100), "action": ("動作", 190), "days": ("曜日", 80),
         "times": ("時刻", 140), "keep": ("世代", 70), "enabled": ("有効", 80)}
KINDS = {"ark-all": "🦖 ARK全", "ark-players": "🧬 プレイヤー",
         "ark": "🦖 ARK", "mc": "🟩/🐑"}


class SchedView(BaseView):
    title = "⏰ 予約"

    def build(self) -> None:
        self._rows: list[dict] = []
        self._targets: list[tuple[str, str, str, str]] = []
        self.head("⏰ 予約 (バックアップ / 更新 / 再起動)",
                  "予約は常駐サービスが実行します。この画面を閉じても動き続けます。\n"
                  "「バックアップ → 更新 → 再起動」の順に実行。"
                  "更新は「更新がある時だけ」停止→更新→元が稼働中なら起動します。")
        self.tree = make_tree(self, COLS, HEADS, "対象", 220, height=12)
        self.tree.bind("<Double-1>", lambda _e: self._edit())
        tb = ToolBar(self)
        tb.pack(fill=tk.X, pady=(8, 0))
        tb.add("＋ 追加", lambda: self._edit(new=True), "Accent.TButton")
        tb.add("✎ 編集", self._edit)
        tb.add("⏯ 有効 / 無効", self._toggle)
        tb.add("🗑 削除", self._delete, "Danger.TButton")
        tb.add("▶ 今すぐ実行", self._run_now)
        self._load_targets()
        self.poll(self.client.schedules, self._fill, interval_ms=6000)

    def _load_targets(self) -> None:
        def done(data, error):
            if error:
                return
            ark, servers = data
            t = [("🦖 ARK 全マップ(停止中は自動スキップ)", "ark-all", "*", "ARK 全マップ"),
                 ("🧬 ARK プレイヤーデータのみ(全マップ+クラスタ・軽量)",
                  "ark-players", "*", "ARK プレイヤーデータ")]
            for a in ark:
                t.append((f"🦖 {a['display_name']}", "ark", a["map_label"],
                          a["display_name"]))
            for s in servers:
                icon = "🐑" if s["game"] == "palworld" else "🟩"
                t.append((f"{icon} {s['display_name']}", "mc", s["name"],
                          s["display_name"]))
            self._targets = t
        self.worker.submit(lambda: (self.client.ark(), self.client.servers()), done)

    def _fill(self, rows) -> None:
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
        fill_tree(self.tree, out)

    def _sel(self):
        return selected_row(self, self.tree, self._rows, "id", "予約")

    def _save(self, rows) -> None:
        def done(_r, error):
            if error:
                show_error(self, error, "予約の保存")
            else:
                self.worker.submit(self.client.schedules,
                                   lambda r, e: self._fill(r) if e is None else None)
        self.worker.submit(lambda: self.client.save_schedules(rows), done)

    def _toggle(self) -> None:
        j = self._sel()
        if j:
            self._save([dict(r, enabled=(not r["enabled"]) if r["id"] == j["id"]
                             else r["enabled"]) for r in self._rows])

    def _delete(self) -> None:
        j = self._sel()
        if j and confirm(self, "確認", f"予約「{j['display']}」を削除しますか?"):
            self._save([r for r in self._rows if r["id"] != j["id"]])

    def _run_now(self) -> None:
        j = self._sel()
        if not j or not confirm(
                self, "確認", f"「{j['display']}」の{j['action_text']}を今すぐ実行しますか?"):
            return

        def done(_r, error):
            if error:
                show_error(self, error, "実行")
            else:
                from tkinter import messagebox
                messagebox.showinfo("実行", "実行しました。📋タスク画面で進捗を確認できます。",
                                    parent=self)
        self.worker.submit(lambda: self.client.run_schedule(j["id"]), done)

    def _edit(self, new: bool = False) -> None:
        from tkinter import messagebox
        job = None if new else self._sel()
        if not new and job is None:
            return
        if not self._targets:
            messagebox.showinfo("準備中", "対象一覧を取得中です。少し待って再試行してください。",
                                parent=self)
            return
        from .sched_app import SchedDialog
        SchedDialog(self.winfo_toplevel(), job, self._targets, self._on_ok)

    def _on_ok(self, job: dict) -> None:
        rows = [r for r in self._rows if r["id"] != job["id"]]
        rows.append(job)
        self._save(rows)
