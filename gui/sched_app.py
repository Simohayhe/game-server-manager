"""予約(スケジューラ)専用GUI。

予約の実体は常駐サービスが持つので、この画面を閉じても予約は動き続ける
(旧GUIは画面を閉じると予約ごと止まっていた)。
"""
from __future__ import annotations

import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from core.scheduler import WEEKDAY_LABELS, normalize_time

from .common import (DEFAULT_BASE, PAL, BaseApp, confirm, fill_tree, hint, make_tree,
                     selected_row, show_error)

INTERVALS = [("使わない(時刻指定)", 0)] + [(f"{m}分毎", m)
                                          for m in (10, 20, 30, 40, 50, 60, 90, 120)]
COLS = ("kind", "action", "days", "times", "keep", "enabled")
HEADS = {"kind": ("種別", 90), "action": ("動作", 180), "days": ("曜日", 80),
         "times": ("時刻", 130), "keep": ("世代", 60), "enabled": ("有効", 80)}


class SchedApp(BaseApp):
    def __init__(self, base: str):
        self._rows: list[dict] = []
        self._targets: list[tuple[str, str, str, str]] = []
        super().__init__(base, "GSM — 予約(バックアップ/更新/再起動)", size="1020x640",
                         minsize=(900, 520))
        self._load_targets()
        self.poll(self.client.schedules, self._fill, interval_ms=6000)

    def build_tabs(self) -> None:
        f = self.add_tab("⏰ 予約")
        hint(f, "↳ 予約は常駐サービスが実行します。この画面を閉じても動き続けます。\n"
                "  「バックアップ → 更新 → 再起動」の順に実行。更新は"
                "「更新がある時だけ」停止→更新→元が稼働中なら起動します。")
        self.tree = make_tree(f, COLS, HEADS, first_head="対象", first_width=200,
                              height=12)
        self.tree.bind("<Double-1>", lambda _e: self._edit())

        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(bar, text="＋ 追加", command=lambda: self._edit(new=True)).pack(side=tk.LEFT)
        ttk.Button(bar, text="✎ 編集", command=self._edit).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="⏯ 有効/無効", command=self._toggle).pack(side=tk.LEFT)
        ttk.Button(bar, text="🗑 削除", command=self._delete).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="▶ 今すぐ実行", command=self._run_now).pack(side=tk.RIGHT)

    # ---- 対象一覧(ARK/サーバー)をサービスから取る ----
    def _load_targets(self) -> None:
        def done(data, error):
            if error:
                return
            ark, servers = data
            t: list[tuple[str, str, str, str]] = [
                ("🦖 ARK 全マップ(停止中は自動スキップ)", "ark-all", "*", "ARK 全マップ"),
                ("🧬 ARK プレイヤーデータのみ(全マップ+クラスタ・軽量)",
                 "ark-players", "*", "ARK プレイヤーデータ"),
            ]
            for a in ark:
                t.append((f"🦖 {a['display_name']}", "ark", a["map_label"],
                          a["display_name"]))
            for s in servers:
                icon = "🐑" if s["game"] == "palworld" else "🟩"
                t.append((f"{icon} {s['display_name']}", "mc", s["name"],
                          s["display_name"]))
            self._targets = t
        self.worker.submit(lambda: (self.client.ark(), self.client.servers()), done)

    def _fill(self, rows: list[dict]) -> None:
        self._rows = rows
        kinds = {"ark-all": "🦖ARK全", "ark-players": "🧬プレイヤー",
                 "ark": "🦖ARK", "mc": "🟩/🐑"}
        out = []
        for j in rows:
            nxt = j.get("next_interval_in_sec")
            times = j["times_text"] + (f" (次{nxt / 60:.0f}分)" if nxt else "")
            out.append((j["id"], j["display"],
                        (kinds.get(j["kind"], j["kind"]), j["action_text"],
                         j["days_text"], times, j["keep"] or "既定",
                         "✅ 有効" if j["enabled"] else "⏸ 無効"),
                        ("active" if j["enabled"] else "off",)))
        fill_tree(self.tree, out)

    def _selected(self) -> dict | None:
        return selected_row(self, self.tree, self._rows, "id", "予約")

    def _save(self, rows: list[dict]) -> None:
        def done(_r, error):
            if error:
                show_error(self, error, "予約の保存")
            else:                       # 保存できたら即座に画面へ反映(pollを待たない)
                self.worker.submit(self.client.schedules,
                                   lambda r, e: self._fill(r) if e is None else None)
        self.worker.submit(lambda: self.client.save_schedules(rows), done)

    def _toggle(self) -> None:
        j = self._selected()
        if not j:
            return
        rows = [dict(r, enabled=(not r["enabled"]) if r["id"] == j["id"] else r["enabled"])
                for r in self._rows]
        self._save(rows)

    def _delete(self) -> None:
        j = self._selected()
        if not j:
            return
        if not confirm(self, "確認", f"予約「{j['display']}」を削除しますか?"):
            return
        self._save([r for r in self._rows if r["id"] != j["id"]])

    def _run_now(self) -> None:
        j = self._selected()
        if not j:
            return
        if not confirm(self, "確認",
                       f"「{j['display']}」の{j['action_text']}を今すぐ実行しますか?"):
            return

        def done(_r, error):
            if error:
                show_error(self, error, "実行")
            else:
                messagebox.showinfo("実行", "実行しました。📋タスクタブで進捗を確認できます。",
                                    parent=self)
        self.worker.submit(lambda: self.client.run_schedule(j["id"]), done)

    # ---- 追加/編集 ----
    def _edit(self, new: bool = False) -> None:
        job = None if new else self._selected()
        if not new and job is None:
            return
        if not self._targets:
            messagebox.showinfo("準備中", "対象一覧を取得中です。少し待って再試行してください。",
                                parent=self)
            return
        SchedDialog(self, job, self._targets, self._on_dialog_ok)

    def _on_dialog_ok(self, job: dict) -> None:
        rows = [r for r in self._rows if r["id"] != job["id"]]
        rows.append(job)
        self._save(rows)


class SchedDialog(tk.Toplevel):
    def __init__(self, master, job: dict | None, targets, on_ok):
        super().__init__(master)
        self.title("予約の編集" if job else "予約の追加")
        self.transient(master)
        self.grab_set()
        self.geometry("560x560")
        self.on_ok = on_ok
        self.job = job
        self.targets = targets
        f = ttk.Frame(self, padding=12)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="対象:").grid(row=0, column=0, sticky=tk.W, pady=4)
        labels = [t[0] for t in targets]
        self.tgt = tk.StringVar()
        combo = ttk.Combobox(f, textvariable=self.tgt, values=labels,
                             state="readonly", width=44)
        combo.grid(row=0, column=1, sticky=tk.W, pady=4)
        idx = 0
        if job:
            for i, t in enumerate(targets):
                if t[1] == job["kind"] and t[2] == job["target"]:
                    idx = i
                    break
        combo.current(idx)

        ttk.Label(f, text="時刻:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.times = tk.StringVar(value=", ".join(job["times"]) if job else "04:00")
        ttk.Entry(f, textvariable=self.times, width=44).grid(row=1, column=1,
                                                             sticky=tk.W, pady=4)
        ttk.Label(f, foreground=PAL["muted"], text="HH:MM をカンマ区切りで複数可"
                  ).grid(row=2, column=1, sticky=tk.W)

        ttk.Label(f, text="曜日:").grid(row=3, column=0, sticky=tk.W, pady=(8, 2))
        df = ttk.Frame(f)
        df.grid(row=3, column=1, sticky=tk.W, pady=(8, 2))
        cur_days = set(job["days"]) if job else set()
        self.days = []
        for i, lbl in enumerate(WEEKDAY_LABELS):
            v = tk.BooleanVar(value=(i in cur_days))
            ttk.Checkbutton(df, text=lbl, variable=v).pack(side=tk.LEFT)
            self.days.append(v)
        ttk.Label(f, foreground=PAL["muted"], text="何も選ばない=毎日"
                  ).grid(row=4, column=1, sticky=tk.W)

        ttk.Label(f, text="動作:").grid(row=5, column=0, sticky=tk.NW, pady=(8, 2))
        af = ttk.Frame(f)
        af.grid(row=5, column=1, sticky=tk.W, pady=(8, 2))
        self.b = tk.BooleanVar(value=job["do_backup"] if job else False)
        self.u = tk.BooleanVar(value=job["do_update"] if job else False)
        self.r = tk.BooleanVar(value=job["do_restart"] if job else True)
        ttk.Checkbutton(af, text="💾 バックアップ", variable=self.b).pack(anchor=tk.W)
        ttk.Checkbutton(af, text="⬆ アップデート(更新があれば適用・ARKのみ)",
                        variable=self.u).pack(anchor=tk.W)
        ttk.Checkbutton(af, text="🔁 再起動", variable=self.r).pack(anchor=tk.W)
        ttk.Label(af, foreground=PAL["muted"], justify=tk.LEFT,
                  text="「バックアップ → 更新 → 再起動」の順。前段が60分を超えたら\n"
                       "再起動は中止されます(予約時刻から離れた再起動を防ぐため)."
                  ).pack(anchor=tk.W)

        self.rolling = tk.BooleanVar(value=job["rolling"] if job else False)
        ttk.Checkbutton(f, text="🔄 ローリング(1台ずつ順に・前が復帰してから次へ)",
                        variable=self.rolling).grid(row=6, column=0, columnspan=2,
                                                    sticky=tk.W, pady=(8, 0))
        ttk.Label(f, foreground=PAL["muted"],
                  text="OFFならマップ毎に並列実行(1台の長い更新が他を止めない)"
                  ).grid(row=7, column=0, columnspan=2, sticky=tk.W)

        ttk.Separator(f, orient=tk.HORIZONTAL).grid(row=8, column=0, columnspan=2,
                                                    sticky=tk.EW, pady=8)
        ttk.Label(f, text="⏱ 定期実行:").grid(row=9, column=0, sticky=tk.W, pady=4)
        self.iv = tk.StringVar()
        ivc = ttk.Combobox(f, textvariable=self.iv, state="readonly", width=26,
                           values=[x[0] for x in INTERVALS])
        ivc.grid(row=9, column=1, sticky=tk.W, pady=4)
        cur_iv = job["interval_min"] if job else 0
        ivc.current(next((i for i, x in enumerate(INTERVALS) if x[1] == cur_iv), 0))
        ttk.Label(f, foreground=PAL["muted"], justify=tk.LEFT,
                  text="設定するとN分毎に実行(時刻/曜日は無視)。\n"
                       "🧬プレイヤーデータのみ を選べば saveworldせず軽量=無停止。"
                  ).grid(row=10, column=0, columnspan=2, sticky=tk.W)

        ttk.Label(f, text="保持世代数:").grid(row=11, column=0, sticky=tk.W, pady=4)
        self.keep = tk.StringVar(value=str(job["keep"]) if (job and job["keep"]) else "")
        ttk.Entry(f, textvariable=self.keep, width=10).grid(row=11, column=1,
                                                            sticky=tk.W, pady=4)
        ttk.Label(f, foreground=PAL["muted"], text="空欄=既定。例: 60 → 10分毎なら10時間ぶん"
                  ).grid(row=12, column=0, columnspan=2, sticky=tk.W)

        self.enabled = tk.BooleanVar(value=job["enabled"] if job else True)
        ttk.Checkbutton(f, text="有効", variable=self.enabled).grid(
            row=13, column=0, columnspan=2, sticky=tk.W, pady=6)

        bar = ttk.Frame(f)
        bar.grid(row=14, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(bar, text="保存", command=self._ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="キャンセル", command=self.destroy).pack(side=tk.LEFT)

    def _ok(self) -> None:
        interval = INTERVALS[[x[0] for x in INTERVALS].index(self.iv.get())][1]
        keep_raw = self.keep.get().strip()
        if keep_raw and not keep_raw.isdigit():
            messagebox.showerror("入力エラー", "保持世代数は数字で入力してください",
                                 parent=self)
            return
        tgt = self.targets[[t[0] for t in self.targets].index(self.tgt.get())]
        if interval > 0:
            times, days = [], []
            do_b, do_u, do_r = True, False, False
        else:
            times = [t for t in (normalize_time(x)
                                 for x in self.times.get().split(",") if x.strip()) if t]
            if not times:
                messagebox.showerror("入力エラー",
                                     "時刻を HH:MM で1つ以上入力してください\n"
                                     "(または「⏱ 定期実行」で間隔を選んでください)",
                                     parent=self)
                return
            days = [i for i, v in enumerate(self.days) if v.get()]
            do_b, do_u, do_r = self.b.get(), self.u.get(), self.r.get()
            if not (do_b or do_u or do_r):
                messagebox.showerror("入力エラー",
                                     "「バックアップ」「更新」「再起動」の少なくとも1つを選んでください",
                                     parent=self)
                return
        self.on_ok({
            "id": self.job["id"] if self.job else uuid.uuid4().hex[:8],
            "kind": tgt[1], "target": tgt[2], "display": tgt[3],
            "times": times, "days": days, "enabled": self.enabled.get(),
            "do_backup": do_b, "do_update": do_u, "do_restart": do_r,
            "rolling": self.rolling.get(),
            "order": self.job["order"] if self.job else [],
            "interval_min": interval, "keep": int(keep_raw) if keep_raw else 0,
        })
        self.destroy()


def run(base: str = "http://127.0.0.1:8770") -> None:
    SchedApp(base).mainloop()
