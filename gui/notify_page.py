"""Discord通知の設定ページ(customtkinter)。複数送信先＋送信先ごとの通知内容選択。

送信先(Discordチャンネル)を何個でも足せて、それぞれ「何を通知するか」を個別に選べる。
例: 管理用チャンネルには全部、みんなの雑談チャンネルには入退室だけ、といった振り分け。
設定は常駐サービスの /api/notify に保存し、サービスは即読み直す(再起動不要)。
"""
from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from .app_ctk import Page
from .widgets import ACCENT, CARD, ERR, MUTED, OK, TEXT


class DestCard(ctk.CTkFrame):
    """送信先1つ分のカード。名前 / URL / 有効 / 通知内容チェック / テスト・削除。"""

    def __init__(self, master, dest: dict, event_labels: dict, game_labels: dict,
                 on_delete, on_test):
        super().__init__(master, fg_color=CARD, corner_radius=10)
        self.event_labels = event_labels
        self.game_labels = game_labels
        self.on_delete = on_delete
        self.on_test = on_test

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.enabled = ctk.CTkSwitch(head, text="", width=44,
                                     onvalue=True, offvalue=False)
        self.enabled.pack(side="left")
        (self.enabled.select if dest.get("enabled", True) else self.enabled.deselect)()
        self.name = ctk.CTkEntry(head, placeholder_text="送信先の名前(例: 管理用)",
                                 width=220, height=30)
        self.name.insert(0, dest.get("name", ""))
        self.name.pack(side="left", padx=8)
        ctk.CTkButton(head, text="🗑 削除", width=70, height=30, corner_radius=6,
                      fg_color="#3a2226", hover_color="#4d2a30",
                      command=lambda: on_delete(self)).pack(side="right")
        ctk.CTkButton(head, text="✈ テスト送信", width=100, height=30, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=lambda: on_test(self)).pack(side="right", padx=6)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=2)
        ctk.CTkLabel(row, text="Webhook URL", text_color=MUTED, width=96,
                     anchor="w", font=ctk.CTkFont(size=11)).pack(side="left")
        self.url = ctk.CTkEntry(row, placeholder_text="https://discord.com/api/webhooks/…",
                                height=30)
        self.url.insert(0, dest.get("webhook_url", ""))
        self.url.pack(side="left", fill="x", expand=True, padx=(4, 0))

        ctk.CTkLabel(self, text="通知するゲーム(チャンネルをゲームごとに分けている場合)",
                     text_color=MUTED, anchor="w", font=ctk.CTkFont(size=11)).pack(
                         fill="x", padx=12, pady=(8, 2))
        grow = ctk.CTkFrame(self, fg_color="transparent")
        grow.pack(fill="x", padx=12, pady=(0, 2))
        self.game_vars: dict[str, ctk.CTkCheckBox] = {}
        gm = dest.get("games", {})
        for i, (key, label) in enumerate(game_labels.items()):
            cb = ctk.CTkCheckBox(grow, text=label, checkbox_width=18, checkbox_height=18,
                                 font=ctk.CTkFont(size=12))
            if gm.get(key, True):
                cb.select()
            cb.grid(row=0, column=i, sticky="w", padx=(0, 18), pady=3)
            self.game_vars[key] = cb

        ctk.CTkLabel(self, text="このチャンネルに通知する内容", text_color=MUTED,
                     anchor="w", font=ctk.CTkFont(size=11)).pack(
                         fill="x", padx=12, pady=(8, 2))
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(0, 12))
        self.ev_vars: dict[str, ctk.CTkCheckBox] = {}
        ev = dest.get("events", {})
        for i, (key, label) in enumerate(event_labels.items()):
            cb = ctk.CTkCheckBox(grid, text=label, checkbox_width=18, checkbox_height=18,
                                 font=ctk.CTkFont(size=12))
            if ev.get(key, False):
                cb.select()
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=3)
            self.ev_vars[key] = cb

    def collect(self) -> dict:
        return {
            "name": self.name.get().strip() or "送信先",
            "webhook_url": self.url.get().strip(),
            "enabled": bool(self.enabled.get()),
            "events": {k: bool(cb.get()) for k, cb in self.ev_vars.items()},
            "games": {k: bool(cb.get()) for k, cb in self.game_vars.items()},
        }


class NotifyPage(Page):
    def build(self):
        self.event_labels: dict = {}
        self.game_labels: dict = {}
        self._cards: list[DestCard] = []
        self.title("🔔 Discord通知")

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        self.master_sw = ctk.CTkSwitch(top, text="Discordへ通知する(全体スイッチ)",
                                       onvalue=True, offvalue=False,
                                       font=ctk.CTkFont(size=13))
        self.master_sw.pack(side="left")
        ctk.CTkLabel(self, text="送信先を複数持てます。送信先ごとに通知する内容を選べます。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(4, 8))

        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list.pack(fill="both", expand=True)

        bar = self.bar()
        self.btn(bar, "＋ 送信先を追加", self._add, "normal").pack(side="left")
        self.btn(bar, "💾 保存", self._save, "primary").pack(side="right")
        ctk.CTkLabel(self, text="Discordのチャンネル設定 → 連携 → Webhook でURLを発行して貼り付け。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(8, 0))
        self._load()

    def _load(self):
        def done(res, err):
            if err:
                messagebox.showerror("通知設定の取得", str(err), parent=self)
                return
            self.event_labels = res.get("events") or {}
            self.game_labels = res.get("games") or {}
            cfg = res.get("config") or {}
            (self.master_sw.select if cfg.get("enabled") else self.master_sw.deselect)()
            for d in (cfg.get("destinations") or []):
                self._add(d)
            if not self._cards:                 # 空なら1枠だけ用意しておく
                self._add()
        self.worker.submit(self.client.notify_get, done)

    def _add(self, dest: dict | None = None):
        if not isinstance(dest, dict):
            # 新規は原則すべてON。ただし port(開閉)はうるさいので既定OFF。ゲームは全部ON。
            dest = {"name": "", "webhook_url": "", "enabled": True,
                    "events": {k: (k != "port") for k in self.event_labels},
                    "games": {k: True for k in self.game_labels}}
        card = DestCard(self.list, dest, self.event_labels, self.game_labels,
                        self._delete, self._test)
        card.pack(fill="x", pady=6)
        self._cards.append(card)

    def _delete(self, card: DestCard):
        self._cards.remove(card)
        card.destroy()

    def _test(self, card: DestCard):
        url = card.url.get().strip()
        if not url:
            messagebox.showinfo("URL未設定", "Webhook URLを入力してください", parent=self)
            return
        name = card.name.get().strip() or "送信先"

        def done(_r, err):
            if err:
                messagebox.showerror("テスト送信失敗", str(err), parent=self)
            else:
                self.app.toast(f"「{name}」にテスト送信しました")
        self.worker.submit(
            lambda: self.client.notify_test(url, f"✅ GSM テスト送信({name})"), done)

    def _save(self):
        cfg = {"enabled": bool(self.master_sw.get()),
               "destinations": [c.collect() for c in self._cards]}

        def done(_r, err):
            if err:
                messagebox.showerror("保存エラー", str(err), parent=self)
            else:
                self.app.toast("Discord通知の設定を保存しました(即反映)")
        self.worker.submit(lambda: self.client.notify_save(cfg), done)
