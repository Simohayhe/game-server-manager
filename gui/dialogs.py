"""再利用ダイアログ(customtkinter)。RCONコンソールなど、各ゲーム画面から開く小窓。"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from .widgets import ACCENT, CARD, MUTED, TEXT


def ark_settings_tabs():
    """ARK_SETTINGS_TABS を SettingsEditor 用の形へ正規化する。"""
    from .settings_specs import ARK_SETTINGS_TABS
    tabs = []
    for tabname, items in ARK_SETTINGS_TABS:
        fields = [{"id": f"{fk}:{section}:{key}", "type": typ,
                   "label": label, "default": default}
                  for fk, section, key, typ, label, default in items]
        tabs.append((tabname, fields))
    return tabs


def pal_settings_tabs():
    """PAL_SETTINGS_TABS を SettingsEditor 用の形へ正規化する。"""
    from .settings_specs import PAL_SETTINGS_TABS
    tabs = []
    for tabname, items in PAL_SETTINGS_TABS:
        fields = []
        for key, typ, label, default, choices in items:
            f = {"id": key, "type": ("choice" if choices else typ),
                 "label": label, "default": default}
            if choices:
                f["choices"] = choices
            fields.append(f)
        tabs.append((tabname, fields))
    return tabs


class RconConsole(ctk.CTkToplevel):
    """RCONの手動コマンド送信コンソール。send_fn(cmd)->str を worker 経由で実行する。"""

    def __init__(self, master, title: str, worker, send_fn, hints=None):
        super().__init__(master)
        self.title(f"RCON — {title}")
        self.geometry("640x460")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.send_fn = send_fn

        ctk.CTkLabel(self, text=f"💬 {title}", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        ctk.CTkLabel(self, text="コマンドを入力して Enter。応答がそのまま表示されます。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        wrap = ctk.CTkFrame(self, fg_color="#12151a", corner_radius=8)
        wrap.pack(fill="both", expand=True, padx=12, pady=10)
        self.out = tk.Text(wrap, bg="#12151a", fg="#c9d1d9", wrap="word", relief="flat",
                           borderwidth=0, padx=10, pady=8, font=("Consolas", 11),
                           state="disabled")
        self.out.pack(fill="both", expand=True, side="left", padx=(2, 0), pady=2)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.out.yview)
        sb.pack(side="right", fill="y", pady=2, padx=(0, 2))
        self.out.configure(yscrollcommand=sb.set)

        if hints:                    # よく使うコマンドのボタン(任意)
            hb = ctk.CTkFrame(self, fg_color="transparent")
            hb.pack(fill="x", padx=12)
            for label, cmd in hints:
                ctk.CTkButton(hb, text=label, width=1, height=26, corner_radius=6,
                              fg_color="#2b303a", hover_color="#39404d",
                              font=ctk.CTkFont(size=11),
                              command=lambda c=cmd: self._send(c)).pack(side="left",
                                                                        padx=(0, 6))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(4, 12))
        self.entry = ctk.CTkEntry(row, placeholder_text="RCONコマンド…", height=34)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._send())
        ctk.CTkButton(row, text="送信", width=70, height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._send).pack(side="left", padx=(8, 0))
        self.entry.focus_set()
        self.after(120, self.lift)

    def _append(self, text: str, prefix: str = "") -> None:
        self.out.configure(state="normal")
        if self.out.index("end-1c") != "1.0":
            self.out.insert("end", "\n")
        self.out.insert("end", prefix + text)
        self.out.configure(state="disabled")
        self.out.see("end")

    def _send(self, cmd: str | None = None) -> None:
        cmd = (cmd if cmd is not None else self.entry.get()).strip()
        if not cmd:
            return
        self.entry.delete(0, "end")
        self._append(cmd, "> ")

        def done(res, err):
            if not self.winfo_exists():
                return
            self._append(str(err) if err else (res or "(応答なし)"),
                         "  " if not err else "  ⚠ ")
        self.worker.submit(lambda: self.send_fn(cmd), done)


class BackupDialog(ctk.CTkToplevel):
    """バックアップ一覧 + 今すぐバックアップ + 選択を復元(ARK/MC/Palworld共通)。"""

    def __init__(self, master, title: str, worker, list_fn, backup_fn, restore_fn,
                 note: str = ""):
        super().__init__(master)
        self.title(f"バックアップ — {title}")
        self.geometry("560x480")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.list_fn = list_fn
        self.backup_fn = backup_fn
        self.restore_fn = restore_fn
        self._rows: list[dict] = []

        ctk.CTkLabel(self, text=f"💾 {title}", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        if note:
            ctk.CTkLabel(self, text=note, text_color=MUTED, anchor="w", wraplength=520,
                         justify="left", font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                         padx=14)
        wrap = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8)
        wrap.pack(fill="both", expand=True, padx=12, pady=10)
        self.tree = ttk.Treeview(wrap, columns=("size", "date"), show="tree headings",
                                 height=12, selectmode="browse", style="D.Treeview")
        self.tree.heading("#0", text="ファイル")
        self.tree.column("#0", width=280)
        self.tree.heading("size", text="サイズ")
        self.tree.column("size", width=90, anchor="center")
        self.tree.heading("date", text="日時")
        self.tree.column("date", width=140, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left", padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                           style="D.Vertical.TScrollbar")
        sb.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self.tree.configure(yscrollcommand=sb.set)

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(bar, text="💾 今すぐバックアップ", height=32, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._backup).pack(side="left")
        ctk.CTkButton(bar, text="↩ 選択を復元", height=32, corner_radius=6,
                      fg_color="#3a2226", hover_color="#4d2a30",
                      command=self._restore).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="再読込", height=32, width=70, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._load).pack(side="right")
        self.after(120, self.lift)
        self._load()

    def _load(self):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"一覧の取得に失敗: {err}")
                return
            self._rows = res or []
            self.tree.delete(*self.tree.get_children())
            for i, b in enumerate(self._rows):
                self.tree.insert("", "end", iid=str(i), text=b["name"],
                                 values=(f"{b['size_mb']} MB", b["mtime"]))
            self.status.configure(text=f"{len(self._rows)}件のバックアップ")
        self.worker.submit(self.list_fn, done)

    def _backup(self):
        self.status.configure(text="バックアップを開始しました(📋タスクで進捗)…")

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("バックアップ", str(err), parent=self)
            else:
                self.after(4000, self._load)
        self.worker.submit(self.backup_fn, done)

    def _restore(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "復元するバックアップを選んでください", parent=self)
            return
        b = self._rows[int(sel[0])]
        if not messagebox.askyesno("復元の確認",
                f"「{b['name']}」で現在のデータを上書きします。よろしいですか?\n"
                "(サーバーは停止しておくのが安全です)", icon="warning",
                default="no", parent=self):
            return

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("復元", str(err), parent=self)
            else:
                self.status.configure(text="復元を開始しました(📋タスクで進捗)")
        self.worker.submit(lambda: self.restore_fn(b["path"]), done)


def _rel_time(epoch) -> str:
    """epoch秒 → 「◯分前」等の相対表記。"""
    import time
    if not epoch:
        return ""
    d = max(0, int(time.time() - float(epoch)))
    if d < 60:
        return "たった今"
    if d < 3600:
        return f"{d // 60}分前"
    if d < 86400:
        return f"{d // 3600}時間前"
    return f"{d // 86400}日前"


class PlayerRestoreDialog(ctk.CTkToplevel):
    """ARK プレイヤーデータ復元。左=バックアップ(時点)一覧、右=その中のプレイヤー一覧。

    「このプレイヤーを◯分前の状態に」= 時点を選ぶ → プレイヤーを選ぶ → 復元。
    名前/キャラ名/レベル/所属マップを表示するので、cryptic なIDでなく人で選べる。
    """

    def __init__(self, master, worker, list_backups_fn, list_players_fn, restore_fn):
        super().__init__(master)
        self.title("プレイヤーデータ復元 — ARK")
        self.geometry("820x560")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.list_backups_fn = list_backups_fn
        self.list_players_fn = list_players_fn
        self.restore_fn = restore_fn
        self._backups: list[dict] = []
        self._players: list[dict] = []
        self._cur_file = None

        ctk.CTkLabel(self, text="🧬 プレイヤーデータ復元", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            self, text="① 復元したい時点(バックアップ)を選ぶ → ② 戻したいプレイヤーを選ぶ → 復元。"
            "\n復元は現在のデータを上書きします(実行前に自動で安全バックアップを取ります)。"
            "マップ停止は不要ですが、対象プレイヤーは復元中オフラインにしてください。",
            text_color=MUTED, anchor="w", justify="left",
            font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # 左: バックアップ(時点)一覧
        left = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
        left.pack(side="left", fill="both", expand=False, padx=(0, 6))
        ctk.CTkLabel(left, text="時点(バックアップ)", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        self.bk_tree = ttk.Treeview(left, columns=("when",), show="tree headings",
                                    height=18, selectmode="browse", style="D.Treeview")
        self.bk_tree.heading("#0", text="日時")
        self.bk_tree.column("#0", width=150)
        self.bk_tree.heading("when", text="いつ")
        self.bk_tree.column("when", width=90, anchor="center")
        self.bk_tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.bk_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_pick_backup())

        # 右: プレイヤー一覧
        right = ctk.CTkFrame(body, fg_color=CARD, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(right, text="この時点に含まれるプレイヤー(複数選択可)", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        self.pl_tree = ttk.Treeview(
            right, columns=("char", "map", "level"), show="tree headings",
            height=18, selectmode="extended", style="D.Treeview")
        self.pl_tree.heading("#0", text="アカウント名")
        self.pl_tree.column("#0", width=180)
        self.pl_tree.heading("char", text="キャラ名")
        self.pl_tree.column("char", width=140)
        self.pl_tree.heading("map", text="マップ")
        self.pl_tree.column("map", width=100, anchor="center")
        self.pl_tree.heading("level", text="Lv")
        self.pl_tree.column("level", width=45, anchor="center")
        self.pl_tree.pack(fill="both", expand=True, side="left", padx=(6, 0), pady=6)
        psb = ttk.Scrollbar(right, orient="vertical", command=self.pl_tree.yview,
                            style="D.Vertical.TScrollbar")
        psb.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self.pl_tree.configure(yscrollcommand=psb.set)

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(bar, text="↩ 選択プレイヤーを復元", height=32, corner_radius=6,
                      fg_color="#3a2226", hover_color="#4d2a30",
                      command=self._restore_selected).pack(side="left")
        ctk.CTkButton(bar, text="↩ この時点を丸ごと復元(全員)", height=32, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._restore_all).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="再読込", height=32, width=70, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._load_backups).pack(side="right")
        self.after(120, self.lift)
        self._load_backups()

    def _load_backups(self):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"BK一覧の取得に失敗: {err}")
                return
            self._backups = res or []
            self.bk_tree.delete(*self.bk_tree.get_children())
            for i, b in enumerate(self._backups):
                self.bk_tree.insert("", "end", iid=str(i), text=b["mtime"],
                                    values=(_rel_time(b.get("epoch")),))
            self.pl_tree.delete(*self.pl_tree.get_children())
            self.status.configure(
                text=f"{len(self._backups)}件の時点。まず左で時点を選択してください。"
                if self._backups else
                "プレイヤーデータのバックアップがありません(先に🧬プレイヤーBKを取ってください)。")
        self.worker.submit(self.list_backups_fn, done)

    def _on_pick_backup(self):
        sel = self.bk_tree.selection()
        if not sel:
            return
        b = self._backups[int(sel[0])]
        self._cur_file = b["path"]
        self.status.configure(text="プレイヤー一覧を読み込み中…")
        self.pl_tree.delete(*self.pl_tree.get_children())

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"プレイヤー一覧の取得に失敗: {err}")
                return
            self._players = res or []
            for i, p in enumerate(self._players):
                acc = p.get("account_name") or f"(不明 {p.get('player_id','')[:8]})"
                self.pl_tree.insert(
                    "", "end", iid=str(i), text=acc,
                    values=(p.get("character_name") or "", p.get("map_label") or "",
                            p.get("level") or ""))
            self.status.configure(
                text=f"{len(self._players)}人。復元したい人を選んで「選択プレイヤーを復元」。")
        self.worker.submit(lambda: self.list_players_fn(self._cur_file), done)

    def _do_restore(self, entries, human):
        when = ""
        sel = self.bk_tree.selection()
        if sel:
            when = _rel_time(self._backups[int(sel[0])].get("epoch"))
        if not messagebox.askyesno(
                "復元の確認",
                f"{human}を「{when}」の状態に戻します。\n"
                "現在のデータは上書きされます(実行前に自動で安全バックアップを取ります)。\n"
                "よろしいですか?", icon="warning", default="no", parent=self):
            return

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("復元", str(err), parent=self)
                self.status.configure(text=f"復元できません: {err}")
            else:
                self.status.configure(text="復元を開始しました(📋タスクで進捗)")
        self.worker.submit(lambda: self.restore_fn(self._cur_file, entries), done)

    def _restore_selected(self):
        if not self._cur_file:
            messagebox.showinfo("時点を選択", "先に左で時点(バックアップ)を選んでください",
                                parent=self)
            return
        sel = self.pl_tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "復元するプレイヤーを選んでください", parent=self)
            return
        chosen = [self._players[int(i)] for i in sel]
        entries = [p["entry"] for p in chosen]
        names = "、".join(p.get("account_name") or p.get("player_id", "")[:8]
                         for p in chosen)
        self._do_restore(entries, f"{len(chosen)}人（{names}）")

    def _restore_all(self):
        if not self._cur_file:
            messagebox.showinfo("時点を選択", "先に左で時点(バックアップ)を選んでください",
                                parent=self)
            return
        self._do_restore(None, "この時点の全プレイヤー")


class ProvisionDialog(ctk.CTkToplevel):
    """新規サーバー構築(Fabric/Forge・MCバージョン選択)。

    既にSSH到達可能なUbuntu VM(空)を用意しておき、ここでゲーム種別/バージョン等を指定すると
    SSHで全自動構築→config追記→一覧に即反映。
    """

    def __init__(self, master, worker, templates_fn, provision_fn, vms_fn=None):
        super().__init__(master)
        self.title("新規サーバー構築")
        self.geometry("560x620")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.templates_fn = templates_fn
        self.provision_fn = provision_fn
        self.vms_fn = vms_fn
        self._templates: list[dict] = []
        self._rows: dict = {}

        ctk.CTkLabel(self, text="⚙ 新規サーバー構築", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            self, text="SSH到達可能な空のUbuntu VMに、指定バージョンで全自動構築します"
            "(Java導入〜systemd化まで)。完了後 config に追記され一覧に出ます。",
            text_color=MUTED, anchor="w", justify="left", wraplength=520,
            font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        form = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=8)
        form.pack(fill="both", expand=True, padx=12, pady=8)

        # テンプレ(ゲーム種別)ドロップダウン
        ctk.CTkLabel(form, text="種別(ローダー)", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        self.tmpl_menu = ctk.CTkOptionMenu(form, values=["(読込中…)"], width=500,
                                           fg_color="#2b303a", button_color="#39404d",
                                           button_hover_color="#4a515e",
                                           command=self._on_tmpl)
        self.tmpl_menu.pack(anchor="w", padx=8, pady=(2, 4))

        self._add_row(form, "mc_version", "MCバージョン", "26.2")
        self._add_row(form, "name", "サーバー名(英数字・config内で一意)", "minecraft4")
        self._add_row(form, "display_name", "表示名", "マイクラ4")
        self._add_row(form, "host", "構築先ホスト(VMのIP)", "192.168.11.103")
        self._add_row(form, "vm", "VM名(任意・自動起動連携に使う)", "mcserver04")
        self._add_row(form, "ssh_user", "SSHユーザー", "master")
        self._add_row(form, "ssh_password", "SSHパスワード", "", secret=True)
        self._add_row(form, "game_port", "ゲームポート", "25565")
        self._add_row(form, "motd", "MOTD(任意)", "A Minecraft Server")

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=520,
                                   justify="left", font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(2, 12))
        ctk.CTkButton(bar, text="⚙ 構築する", height=34, corner_radius=6, fg_color=ACCENT,
                      hover_color="#4a86e0", command=self._build).pack(side="left")
        ctk.CTkButton(bar, text="閉じる", height=34, width=80, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="right")
        self.after(120, self.lift)
        self._load_templates()

    def _add_row(self, parent, key, label, default, secret=False):
        ctk.CTkLabel(parent, text=label, text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        e = ctk.CTkEntry(parent, width=500, show="•" if secret else "")
        if default:
            e.insert(0, default)
        e.pack(anchor="w", padx=8, pady=(2, 0))
        self._rows[key] = e

    def _set(self, key, value):
        e = self._rows[key]
        e.delete(0, "end")
        e.insert(0, str(value))

    def _load_templates(self):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"テンプレ取得に失敗: {err}")
                return
            self._templates = res or []
            labels = [t["label"] for t in self._templates]
            if labels:
                self.tmpl_menu.configure(values=labels)
                self.tmpl_menu.set(labels[0])
                self._on_tmpl(labels[0])
        self.worker.submit(self.templates_fn, done)

    def _cur_template(self):
        lbl = self.tmpl_menu.get()
        return next((t for t in self._templates if t["label"] == lbl), None)

    def _on_tmpl(self, _label):
        t = self._cur_template()
        if not t:
            return
        self._set("mc_version", t.get("mc_version") or "")
        self._set("game_port", t.get("game_port") or 25565)

    def _build(self):
        t = self._cur_template()
        if not t:
            messagebox.showinfo("テンプレ未選択", "種別を選んでください", parent=self)
            return
        v = {k: e.get().strip() for k, e in self._rows.items()}
        for req in ("name", "host", "ssh_user", "ssh_password"):
            if not v.get(req):
                messagebox.showinfo("入力不足", f"「{req}」を入力してください", parent=self)
                return
        if not messagebox.askyesno(
                "構築の確認",
                f"{v['host']} に {t['display_name']} {v['mc_version']} を構築します。\n"
                f"サーバー名: {v['name']}\n数分かかります(📋タスクで進捗)。続けますか?",
                default="no", parent=self):
            return
        body = dict(v, template_id=t["id"])

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"構築を開始できません: {err}")
                messagebox.showerror("構築", str(err), parent=self)
            else:
                self.status.configure(
                    text="構築を開始しました(📋タスクで進捗)。完了すると一覧に出ます。")
        self.worker.submit(lambda: self.provision_fn(**body), done)


class VmCloneDialog(ctk.CTkToplevel):
    """テンプレVMを複製→個体化(hostname/IP変更)して、構築可能な空VMを作る。

    完了後、そのVMを「⚙ 新規構築」の構築先に指定できる。
    """

    def __init__(self, master, worker, clone_fn, vms_fn=None):
        super().__init__(master)
        self.title("VMをクローン(テンプレから新規VM)")
        self.geometry("560x580")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.clone_fn = clone_fn
        self.vms_fn = vms_fn
        self._rows: dict = {}

        ctk.CTkLabel(self, text="📋 VMをクローン", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            self, text="常時停止のテンプレVM(例 ubuntu_template)を複製し、ホスト名とIPを個体化します。"
            "完了後は「⚙ 新規構築」でサーバーを入れられます。",
            text_color=MUTED, anchor="w", justify="left", wraplength=520,
            font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        form = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=8)
        form.pack(fill="both", expand=True, padx=12, pady=8)
        self._add(form, "template", "テンプレVM名", "ubuntu_template")
        self._add(form, "template_ip", "テンプレのIP(起動直後のIP)", "192.168.11.199")
        self._add(form, "new_name", "新VM名", "mcserver04")
        self._add(form, "hostname", "ホスト名", "mcserver04")
        self._add(form, "new_ip", "新IP(第4オクテットのみ可 例 103)", "103")
        self._add(form, "memory_gb", "メモリ(GB)", "4")
        self._add(form, "cpu", "CPU数", "4")
        self._add(form, "ssh_user", "SSHユーザー", "master")
        self._add(form, "ssh_password", "SSHパスワード", "", secret=True)

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=520,
                                   justify="left", font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(2, 12))
        ctk.CTkButton(bar, text="📋 クローンする", height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._clone).pack(side="left")
        ctk.CTkButton(bar, text="閉じる", height=34, width=80, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="right")
        self.after(120, self.lift)

    def _add(self, parent, key, label, default, secret=False):
        ctk.CTkLabel(parent, text=label, text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        e = ctk.CTkEntry(parent, width=500, show="•" if secret else "")
        if default:
            e.insert(0, default)
        e.pack(anchor="w", padx=8, pady=(2, 0))
        self._rows[key] = e

    def _clone(self):
        v = {k: e.get().strip() for k, e in self._rows.items()}
        for req in ("template", "template_ip", "new_name", "hostname", "new_ip",
                    "ssh_user", "ssh_password"):
            if not v.get(req):
                messagebox.showinfo("入力不足", f"「{req}」を入力してください", parent=self)
                return
        if not messagebox.askyesno(
                "クローンの確認",
                f"{v['template']} を複製して {v['new_name']}(IP …{v['new_ip']})を作ります。\n"
                "テンプレVMは停止しておいてください。数分かかります(📋タスクで進捗)。続けますか?",
                default="no", parent=self):
            return

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"開始できません: {err}")
                messagebox.showerror("VMクローン", str(err), parent=self)
            else:
                self.status.configure(
                    text="クローンを開始しました(📋タスクで進捗)。完了後は「⚙ 新規構築」で使えます。")
        self.worker.submit(lambda: self.clone_fn(**v), done)


class PlayerCommandDialog(ctk.CTkToplevel):
    """ARK: 接続中プレイヤーに管理コマンドを1クリックで当てる。

    コマンドを選ぶ → プレイヤーを選ぶ → 実行。クリエイティブは**トグル**なので、
    同じ操作で「付与」も「戻し(解除)」もできる(ON→OFF)。
    ※ ASAのRCONは god/fly 単体を他人に付与できないため、飛行+無敵+無限資源を束ねた
      クリエイティブモード(GiveCreativeModeToPlayer)を使う。
    """

    # (ラベル, コマンド雛形{id}, 確認が要るか)
    COMMANDS = [
        ("🪄 クリエイティブ 切替 (飛行＋無敵＋無限資源) ※もう一度で解除",
         "GiveCreativeModeToPlayer {id}", False),
        ("⭐ 経験値 +10000", "GiveExpToPlayer {id} 10000 0 1", False),
        ("💥 キル (その場で死亡)", "KillPlayer {id}", True),
    ]

    def __init__(self, master, worker, map_name: str, list_fn, run_fn):
        super().__init__(master)
        self.title(f"プレイヤーにコマンド — {map_name}")
        self.geometry("560x460")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.list_fn = list_fn        # () -> ListPlayers 生文字列
        self.run_fn = run_fn          # (cmd) -> 応答
        self._players: list[tuple[str, str]] = []

        ctk.CTkLabel(self, text=f"🎮 プレイヤーにコマンド — {map_name}", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(
            self, text="① コマンドを選ぶ → ② 接続中プレイヤーを選ぶ → 実行。"
            "\nクリエイティブは切替(トグル)なので、同じ操作で解除(戻し)もできます。",
            text_color=MUTED, anchor="w", justify="left",
            font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        ctk.CTkLabel(self, text="コマンド", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14, pady=(8, 0))
        self._cmd_labels = [c[0] for c in self.COMMANDS]
        self.cmd_menu = ctk.CTkOptionMenu(self, values=self._cmd_labels, width=520,
                                          fg_color="#2b303a", button_color="#39404d",
                                          button_hover_color="#4a515e")
        self.cmd_menu.set(self._cmd_labels[0])
        self.cmd_menu.pack(anchor="w", padx=14, pady=(2, 6))

        wrap = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        ctk.CTkLabel(wrap, text="接続中プレイヤー", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(6, 0))
        self.tree = ttk.Treeview(wrap, columns=("id",), show="tree headings",
                                 height=10, selectmode="browse", style="D.Treeview")
        self.tree.heading("#0", text="名前")
        self.tree.column("#0", width=220)
        self.tree.heading("id", text="ID")
        self.tree.column("id", width=290)
        self.tree.pack(fill="both", expand=True, side="left", padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                           style="D.Vertical.TScrollbar")
        sb.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self.tree.configure(yscrollcommand=sb.set)

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(2, 12))
        ctk.CTkButton(bar, text="実行", height=32, corner_radius=6, fg_color=ACCENT,
                      hover_color="#4a86e0", command=self._run).pack(side="left")
        ctk.CTkButton(bar, text="プレイヤー再読込", height=32, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._load).pack(side="right")
        self.after(120, self.lift)
        self._load()

    @staticmethod
    def _parse(raw: str):
        import re
        out = []
        for line in (raw or "").splitlines():
            m = re.match(r"\s*\d+\.\s*(.+?),\s*([0-9A-Za-z]{12,})\s*$", line)
            if m:
                out.append((m.group(1).strip(), m.group(2).strip()))
        return out

    def _load(self):
        self.status.configure(text="接続中プレイヤーを取得中…")

        def done(raw, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"取得に失敗: {err}")
                return
            self._players = self._parse(raw)
            self.tree.delete(*self.tree.get_children())
            for i, (name, pid) in enumerate(self._players):
                self.tree.insert("", "end", iid=str(i), text=name, values=(pid,))
            self.status.configure(
                text=(f"{len(self._players)}人 接続中。対象を選んで実行。"
                      if self._players else
                      "接続中のプレイヤーがいません(コマンドは接続中の人に当てます)。"))
        self.worker.submit(self.list_fn, done)

    def _run(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "対象プレイヤーを選んでください", parent=self)
            return
        name, pid = self._players[int(sel[0])]
        label, tmpl, confirm = self.COMMANDS[self._cmd_labels.index(self.cmd_menu.get())]
        if confirm and not messagebox.askyesno(
                "確認", f"{name} に「{label}」を実行します。よろしいですか?",
                icon="warning", default="no", parent=self):
            return
        cmd = tmpl.format(id=pid)
        self.status.configure(text=f"実行中: {cmd}")

        def done(resp, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"失敗: {err}")
                messagebox.showerror("コマンド", str(err), parent=self)
            else:
                self.status.configure(text=f"✅ {name} に実行: {label}  応答: {(resp or '').strip()[:60]}")
        self.worker.submit(lambda: self.run_fn(cmd), done)


class PropsEditor(ctk.CTkToplevel):
    """server.properties 編集(MC)。キーは動的なので取得してから行を組む。

    true/false の値はスイッチ、それ以外は入力欄。変更した項目だけ保存する。
    """

    def __init__(self, master, title: str, worker, fetch_fn, save_fn):
        super().__init__(master)
        self.title(f"詳細設定 — {title}")
        self.geometry("560x620")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.save_fn = save_fn
        self._rows: dict[str, tuple] = {}     # key -> (widget, is_bool, initial)

        ctk.CTkLabel(self, text=f"⚙ {title} (server.properties)", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        ctk.CTkLabel(self, text="変更した項目だけ保存します。反映にはサーバー再起動が必要です。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)
        self.body = ctk.CTkScrollableFrame(self, fg_color=CARD)
        self.body.pack(fill="both", expand=True, padx=10, pady=8)
        self.status = ctk.CTkLabel(self, text="読み込み中…", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        self.restart_var = ctk.CTkCheckBox(bar, text="保存後に再起動して反映する",
                                           font=ctk.CTkFont(size=11))
        self.restart_var.pack(side="left")
        ctk.CTkButton(bar, text="💾 保存", width=90, height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._save).pack(side="right")
        self.after(120, self.lift)
        self._load(fetch_fn)

    def _load(self, fetch_fn):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"取得失敗: {err}", text_color="#ff8f8f")
                return
            for p in (res or []):
                self._add_row(p["key"], p["value"])
            self.status.configure(text=f"{len(res or [])}項目")
        self.worker.submit(fetch_fn, done)

    def _add_row(self, key, value):
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=key, text_color="#d7dee6", anchor="w", width=230,
                     wraplength=230, justify="left",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(2, 8))
        is_bool = str(value).strip().lower() in ("true", "false")
        if is_bool:
            w = ctk.CTkSwitch(row, text="", width=44)
            (w.select if str(value).strip().lower() == "true" else w.deselect)()
            w.pack(side="right")
            init = "true" if w.get() else "false"
        else:
            w = ctk.CTkEntry(row, width=180, height=28)
            w.insert(0, "" if value is None else str(value))
            w.pack(side="right")
            init = w.get()
        self._rows[key] = (w, is_bool, init)

    def _save(self):
        changes = {}
        for key, (w, is_bool, init) in self._rows.items():
            cur = ("true" if w.get() else "false") if is_bool else w.get()
            if cur != init:
                changes[key] = cur
        if not changes:
            self.status.configure(text="変更はありません。", text_color=MUTED)
            return
        restart = bool(self.restart_var.get())
        self.status.configure(text=f"{len(changes)}項目を保存中…")

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("保存エラー", str(err), parent=self)
            else:
                self.status.configure(text=f"✅ {len(changes)}項目を保存しました",
                                      text_color="#7ee787")
                for k in changes:
                    w, is_bool, _ = self._rows[k]
                    self._rows[k] = (w, is_bool, changes[k])
        self.worker.submit(lambda: self.save_fn(changes, restart), done)


class ArkUpdateDialog(ctk.CTkToplevel):
    """ARK更新: どのマップを更新するか選ぶ + ローリングON/OFF。

    maps = [{index, display_name, version, build, running, outdated}]
    run_fn(indices, rolling) を worker 経由で実行する。
    """

    def __init__(self, master, maps, worker, run_fn):
        super().__init__(master)
        self.title("ARK サーバー更新")
        self.geometry("520x560")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.run_fn = run_fn
        self._vars: dict[int, ctk.CTkCheckBox] = {}

        ctk.CTkLabel(self, text="⬆ 更新するマップを選択", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        ctk.CTkLabel(self, text="🆕=更新あり。更新のあるマップだけ選ぶのが基本です。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(6, 0))
        ctk.CTkButton(head, text="更新ありを全選択", height=26, width=130, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11),
                      command=self._select_outdated).pack(side="left")
        ctk.CTkButton(head, text="全解除", height=26, width=70, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11),
                      command=self._clear).pack(side="left", padx=6)

        body = ctk.CTkScrollableFrame(self, fg_color=CARD)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        for m in maps:
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=2)
            mark = " 🆕" if m.get("outdated") else ""
            run = "🟢" if m.get("running") else "⚪"
            cb = ctk.CTkCheckBox(
                row, text=f"{run} {m['display_name']}   ({m.get('version') or '―'}{mark})",
                font=ctk.CTkFont(size=12))
            if m.get("outdated"):
                cb.select()
            cb.pack(side="left")
            self._vars[m["index"]] = cb

        self.rolling = ctk.CTkCheckBox(
            self, text="ローリング更新(1マップずつ順番に。同時に落ちるのは1つ)",
            font=ctk.CTkFont(size=12))
        self.rolling.select()          # 既定ON(安全側)
        self.rolling.pack(anchor="w", padx=14, pady=(2, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(6, 12))
        ctk.CTkButton(bar, text="閉じる", width=70, height=34, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="left")
        ctk.CTkButton(bar, text="⬆ 更新を実行", height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._run).pack(side="right")
        self.after(120, self.lift)

    def _select_outdated(self):
        for cb in self._vars.values():
            pass  # 下で個別に判定できないので、テキストの🆕で判定
        for idx, cb in self._vars.items():
            (cb.select if "🆕" in cb.cget("text") else cb.deselect)()

    def _clear(self):
        for cb in self._vars.values():
            cb.deselect()

    def _run(self):
        indices = [i for i, cb in self._vars.items() if cb.get()]
        if not indices:
            messagebox.showinfo("選択なし", "更新するマップを選んでください", parent=self)
            return
        rolling = bool(self.rolling.get())
        mode = "ローリング(1つずつ)" if rolling else "並列(同時)"
        if not messagebox.askyesno("更新の確認",
                f"{len(indices)}マップを更新します。方式: {mode}\n"
                "更新のあるマップだけ 停止→更新→起動 します。よろしいですか?",
                parent=self):
            return

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("更新", str(err), parent=self)
            else:
                self.destroy()
        self.worker.submit(lambda: self.run_fn(indices, rolling), done)


class ColorPicker(ctk.CTkToplevel):
    """ARKの色を色見本(スウォッチ)付きで選ぶ。全選択で「全色」も一発。

    on_done(names: list[str]) を選択確定時に呼ぶ。selected=事前に選択済みの色名。
    """

    def __init__(self, master, selected, on_done):
        super().__init__(master)
        self.title("色を選ぶ")
        self.geometry("560x620")
        self.configure(fg_color="#0f1115")
        self.on_done = on_done
        from .ark_colors import ARK_COLORS
        self._colors = ARK_COLORS
        sel = set(selected or [])
        self._vars: dict[str, ctk.CTkCheckBox] = {}

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text="🎨 使う色を選択", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text="全色", height=28, width=64, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=lambda: self._all(True)).pack(side="right")
        ctk.CTkButton(head, text="全解除", height=28, width=64, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=lambda: self._all(False)).pack(side="right", padx=6)

        self.count = ctk.CTkLabel(self, text="", text_color=MUTED,
                                  font=ctk.CTkFont(size=11))
        self.count.pack(anchor="w", padx=14)

        body = ctk.CTkScrollableFrame(self, fg_color=CARD)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        cols = 2
        for i, (name, hexv) in enumerate(self._colors):
            cell = ctk.CTkFrame(body, fg_color="transparent")
            cell.grid(row=i // cols, column=i % cols, sticky="w", padx=6, pady=2)
            sw = ctk.CTkFrame(cell, width=22, height=22, corner_radius=4,
                              fg_color=hexv, border_width=1, border_color="#3a3f48")
            sw.pack(side="left", padx=(0, 6))
            sw.pack_propagate(False)
            cb = ctk.CTkCheckBox(cell, text=name, checkbox_width=18, checkbox_height=18,
                                 font=ctk.CTkFont(size=11), command=self._update_count)
            if name in sel:
                cb.select()
            cb.pack(side="left")
            self._vars[name] = cb

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(bar, text="キャンセル", width=90, height=34, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="left")
        ctk.CTkButton(bar, text="この色で決定", height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._ok).pack(side="right")
        self.after(120, self.lift)
        self._update_count()

    def _all(self, on):
        for cb in self._vars.values():
            (cb.select if on else cb.deselect)()
        self._update_count()

    def _update_count(self):
        n = sum(cb.get() for cb in self._vars.values())
        self.count.configure(text=f"{n} / {len(self._vars)} 色を選択中")

    def _ok(self):
        names = [n for n, cb in self._vars.items() if cb.get()]
        self.on_done(names)
        self.destroy()


class DynConfigDialog(ctk.CTkToplevel):
    """ARK動的設定(無停止で倍率変更)。倍率項目 + カラフル野生恐竜(色)を1画面に。

    get_fn() -> /api/dynconfig の結果(enabled/values/settings/serving)。
    save_fn(values, enabled, respawn) を worker 経由で実行(ForceUpdateDynamicConfigで即反映)。
    """

    PRESET = ("DarkLavender,DarkMagenta,Dino Dark Purple,Dino Light Red,DeepPink,"
              "Dark Red,LemonLime,Red,ActualBlack,Cyan,Dino Light Blue,"
              "Dino Dark Green,BubbleGum,Mint,Dino Light Yellow")

    def __init__(self, master, worker, get_fn, save_fn):
        super().__init__(master)
        self.title("ARK 動的設定")
        self.geometry("600x680")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.save_fn = save_fn
        self._rows: dict[str, tuple] = {}      # key -> (checkbox, entry, default)

        ctk.CTkLabel(self, text="⚡ ARK 動的設定(無停止で反映)", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        ctk.CTkLabel(self, text="チェックした項目だけを上書きします。稼働中マップへ即時反映"
                     "(再起動不要)。有効化後は各マップ一度だけ再起動が必要です。",
                     text_color=MUTED, wraplength=560, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        self.master_sw = ctk.CTkSwitch(self, text="動的設定を有効にする",
                                       onvalue=True, offvalue=False,
                                       font=ctk.CTkFont(size=13))
        self.master_sw.pack(anchor="w", padx=14, pady=(8, 2))
        self.warn = ctk.CTkLabel(self, text="", text_color="#ffc27a",
                                 font=ctk.CTkFont(size=11))
        self.warn.pack(anchor="w", padx=14)

        self.body = ctk.CTkScrollableFrame(self, fg_color=CARD)
        self.body.pack(fill="both", expand=True, padx=10, pady=8)

        # --- カラフル野生恐竜(動的設定の一項目として) ---
        cframe = ctk.CTkFrame(self.body, fg_color="#1a1f27", corner_radius=8)
        cframe.pack(fill="x", pady=(0, 8))
        top = ctk.CTkFrame(cframe, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 2))
        self.colors_on = ctk.CTkCheckBox(top, text="🎨 カラフル野生恐竜(イベント/mod不要)",
                                         font=ctk.CTkFont(size=12, weight="bold"))
        self.colors_on.pack(side="left")
        ctk.CTkButton(top, text="🎨 色を選ぶ", height=24, width=88, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11),
                      command=self._pick_colors).pack(side="right")
        ctk.CTkButton(top, text="全色", height=24, width=56, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11),
                      command=self._all_colors).pack(side="right", padx=6)
        self.colorset = ctk.CTkTextbox(cframe, height=76, fg_color=CARD,
                                       font=ctk.CTkFont(size=11))
        self.colorset.pack(fill="x", padx=10, pady=(2, 6))
        self.respawn = ctk.CTkCheckBox(
            cframe, text="保存後にリスポーンして既存の野生恐竜も色を反映",
            font=ctk.CTkFont(size=11))
        self.respawn.select()
        self.respawn.pack(anchor="w", padx=10, pady=(0, 8))

        self.status = ctk.CTkLabel(self, text="読み込み中…", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(bar, text="閉じる", width=70, height=34, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="left")
        ctk.CTkButton(bar, text="💾 保存して反映", height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._save).pack(side="right")
        self.after(120, self.lift)
        self._load(get_fn)

    def _set_colorset(self, names):
        self.colorset.delete("1.0", "end")
        self.colorset.insert("1.0", ",".join(names))
        if names:
            self.colors_on.select()

    def _all_colors(self):
        from .ark_colors import ALL_NAMES
        self._set_colorset(ALL_NAMES)

    def _pick_colors(self):
        cur = [c.strip() for c in self.colorset.get("1.0", "end").split(",") if c.strip()]
        ColorPicker(self, cur, on_done=self._set_colorset)

    def _add_setting(self, s, cur):
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=2)
        cb = ctk.CTkCheckBox(row, text=s["label"], width=300, checkbox_width=18,
                             checkbox_height=18, font=ctk.CTkFont(size=12))
        cb.pack(side="left", padx=(2, 8))
        ent = ctk.CTkEntry(row, width=120, height=28)
        ent.pack(side="right")
        if cur is not None:                    # 現在上書き中の値
            cb.select()
            ent.insert(0, str(cur))
        else:
            ent.insert(0, str(s["default"]))
        self._rows[s["key"]] = (cb, ent, s["default"])

    def _load(self, get_fn):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"取得失敗: {err}", text_color="#ff8f8f")
                return
            (self.master_sw.select if res.get("enabled") else self.master_sw.deselect)()
            vals = res.get("values") or {}
            for s in (res.get("settings") or []):
                self._add_setting(s, vals.get(s["key"]))
            # 色(valuesから)
            if vals.get("ActiveEventColors", "").lower() == "custom":
                self.colors_on.select()
            self.colorset.insert("1.0", vals.get("DynamicColorset") or self.PRESET)
            if not res.get("serving"):
                self.warn.configure(text="⚠ 現在オフ。有効にすると配信が始まります(色/倍率が効きます)。")
            self.status.configure(text="チェックした項目だけ上書き。稼働中マップへ即反映。",
                                  text_color=MUTED)
        self.worker.submit(get_fn, done)

    def _save(self):
        values = {}
        for key, (cb, ent, default) in self._rows.items():
            if cb.get():
                values[key] = ent.get().strip()
        colors_on = bool(self.colors_on.get())
        if colors_on:
            values["ActiveEventColors"] = "custom"
            values["DynamicColorset"] = self.colorset.get("1.0", "end").strip()
        enabled = bool(self.master_sw.get())
        respawn = bool(self.respawn.get()) and colors_on
        self.status.configure(text="保存して反映中…")

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("保存", str(err), parent=self)
            else:
                self.status.configure(
                    text="✅ 保存＆反映しました"
                    + ("(色反映のためリスポーン中)" if respawn else ""),
                    text_color="#7ee787")
        self.worker.submit(lambda: self.save_fn(values, enabled, respawn), done)


class RawIniEditor(ctk.CTkToplevel):
    """ARKのiniを生テキストで直接編集(上級者向け・配列やキュレート外キー用)。

    GameUserSettings.ini / Game.ini を丸ごと読み込み、そのまま書き戻す。
    レベル別ステ・エングラム解放・スタックサイズ上書き等の配列設定はここで扱う。
    """

    def __init__(self, master, worker, get_fn, save_fn):
        super().__init__(master)
        self.title("ARK 生設定ファイル編集(上級者向け)")
        self.geometry("760x680")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.get_fn = get_fn
        self.save_fn = save_fn
        self.which = "gus"

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(top, text="ファイル:", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.filesel = ctk.CTkSegmentedButton(
            top, values=["GameUserSettings.ini", "Game.ini"],
            command=self._switch)
        self.filesel.set("GameUserSettings.ini")
        self.filesel.pack(side="left", padx=10)
        ctk.CTkLabel(self, text="全マップ共通。配列(PerLevelStatsMultiplier等)もここで編集できます。"
                     "反映には再起動が必要です。保存は丸ごと上書きなので慎重に。",
                     text_color=MUTED, anchor="w", wraplength=700, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14)

        wrap = ctk.CTkFrame(self, fg_color="#12151a", corner_radius=8)
        wrap.pack(fill="both", expand=True, padx=12, pady=8)
        self.text = tk.Text(wrap, bg="#12151a", fg="#c9d1d9", wrap="none", relief="flat",
                            borderwidth=0, padx=10, pady=8, font=("Consolas", 11),
                            insertbackground="#c9d1d9", undo=True)
        self.text.pack(fill="both", expand=True, side="left", padx=(2, 0), pady=2)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        sb.pack(side="right", fill="y", pady=2)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.text.xview)
        hsb.pack(side="bottom", fill="x")
        self.text.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)

        self.status = ctk.CTkLabel(self, text="読み込み中…", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(bar, text="🔄 再読込", width=90, height=32, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._load).pack(side="left")
        ctk.CTkButton(bar, text="💾 保存(全マップ)", height=32, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._save).pack(side="right")
        self.after(120, self.lift)
        self._load()

    def _switch(self, _v):
        self.which = "game" if self.filesel.get() == "Game.ini" else "gus"
        self._load()

    def _load(self):
        self.status.configure(text="読み込み中…")

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"取得失敗: {err}", text_color="#ff8f8f")
                return
            self.text.delete("1.0", "end")
            self.text.insert("1.0", (res or {}).get("text", ""))
            self.text.edit_reset()
            self.status.configure(text=f"{(res or {}).get('path','')}", text_color=MUTED)
        self.worker.submit(lambda: self.get_fn(self.which), done)

    def _save(self):
        content = self.text.get("1.0", "end-1c")
        if not messagebox.askyesno("保存の確認",
                f"{self.filesel.get()} を全マップに丸ごと上書きします。よろしいですか?\n"
                "(構文ミスがあるとその設定が読まれません。反映は再起動後)",
                icon="warning", default="no", parent=self):
            return
        self.status.configure(text="保存中…")

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("保存エラー", str(err), parent=self)
            else:
                self.status.configure(text="✅ 保存しました(反映は各マップ再起動後)",
                                      text_color="#7ee787")
        self.worker.submit(lambda: self.save_fn(self.which, content), done)


class SettingsEditor(ctk.CTkToplevel):
    """タブ付きの設定エディタ(ARK/Palworld共通)。変更した項目だけ保存する。

    tabs: [(タブ名, [field, ...])]  field = {id, type, label, default, choices?}
      type = "float"|"int"|"str"|"bool"|"choice"
    fetch_fn(ids) -> {id: 現在値(文字列) or None}   worker経由で実行
    save_fn(changes: dict, restart: bool) -> 任意   worker経由で実行
    """

    def __init__(self, master, title: str, tabs, worker, fetch_fn, save_fn,
                 note: str = "", restart_label: str | None = None):
        super().__init__(master)
        self.title(title)
        self.geometry("620x640")
        self.configure(fg_color="#0f1115")
        self.worker = worker
        self.fetch_fn = fetch_fn
        self.save_fn = save_fn
        self._widgets: dict[str, tuple] = {}     # id -> (type, widget, initial_shown, choices)
        self._fields: dict[str, dict] = {}

        ctk.CTkLabel(self, text=title, text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 0))
        if note:
            ctk.CTkLabel(self, text=note, text_color=MUTED, anchor="w",
                         wraplength=580, justify="left",
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14, pady=(2, 0))

        self.tabview = ctk.CTkTabview(self, fg_color=CARD)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=8)
        for tabname, fields in tabs:
            self.tabview.add(tabname)
            frame = ctk.CTkScrollableFrame(self.tabview.tab(tabname),
                                           fg_color="transparent")
            frame.pack(fill="both", expand=True)
            for f in fields:
                self._fields[f["id"]] = f
                self._add_field(frame, f)

        self.status = ctk.CTkLabel(self, text="現在値を読み込み中…", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(4, 12))
        self.restart_var = None
        if restart_label:
            self.restart_var = ctk.CTkCheckBox(bar, text=restart_label,
                                               font=ctk.CTkFont(size=11))
            self.restart_var.pack(side="left")
        ctk.CTkButton(bar, text="💾 保存", width=90, height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._save).pack(side="right")
        ctk.CTkButton(bar, text="閉じる", width=70, height=34, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="right", padx=(0, 8))
        self.after(120, self.lift)
        self._load()

    def _add_field(self, parent, f):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=f["label"], text_color="#d7dee6", anchor="w",
                     wraplength=330, justify="left",
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=(2, 8))
        t = f["type"]
        if t == "bool":
            w = ctk.CTkSwitch(row, text="", width=44, onvalue=True, offvalue=False)
            w.pack(side="right")
        elif t == "choice":
            labels = [lb for _v, lb in f["choices"]]
            w = ctk.CTkOptionMenu(row, values=labels, width=180, height=30,
                                  font=ctk.CTkFont(size=11))
            w.pack(side="right")
        else:
            w = ctk.CTkEntry(row, width=140, height=30)
            w.pack(side="right")
        self._widgets[f["id"]] = [t, w, None, f.get("choices")]

    def _load(self):
        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"読み込み失敗: {err}", text_color="#ff8f8f")
                return
            vals = (res or {}).get("values", {})
            for fid, (t, w, _init, choices) in self._widgets.items():
                cur = vals.get(fid)
                shown = cur if cur is not None else self._fields[fid]["default"]
                self._set_widget(t, w, shown, choices)
                self._widgets[fid][2] = self._get_widget(t, w, choices)  # 初期値を記録
            self.status.configure(text="現在値(未設定は既定値)を表示中。変更した項目だけ保存します。",
                                  text_color=MUTED)
        self.worker.submit(lambda: self.fetch_fn(list(self._widgets)), done)

    @staticmethod
    def _set_widget(t, w, value, choices):
        if t == "bool":
            on = str(value).strip().lower() in ("true", "1", "yes")
            (w.select if on else w.deselect)()
        elif t == "choice":
            lab = next((lb for v, lb in choices if v == value), None)
            w.set(lab or choices[0][1])
        else:
            w.delete(0, "end")
            w.insert(0, "" if value is None else str(value))

    @staticmethod
    def _get_widget(t, w, choices):
        if t == "bool":
            return "True" if w.get() else "False"
        if t == "choice":
            lab = w.get()
            return next((v for v, l in choices if l == lab), lab)
        return w.get().strip()

    def _save(self):
        changes = {}
        for fid, (t, w, init, choices) in self._widgets.items():
            cur = self._get_widget(t, w, choices)
            if cur == init:
                continue                     # 未変更 → 書かない(既定値の書き込み肥大を防ぐ)
            if t in ("float", "int"):        # 数値バリデーション
                try:
                    float(cur) if t == "float" else int(cur)
                except ValueError:
                    messagebox.showerror("入力エラー",
                        f"「{self._fields[fid]['label']}」は数値で入力してください: {cur!r}",
                        parent=self)
                    return
            changes[fid] = cur
        if not changes:
            self.status.configure(text="変更はありません。", text_color=MUTED)
            return
        restart = bool(self.restart_var.get()) if self.restart_var else False
        self.status.configure(text=f"{len(changes)}項目を保存中…", text_color=MUTED)

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("保存エラー", str(err), parent=self)
            else:
                self.status.configure(text=f"✅ {len(changes)}項目を保存しました",
                                      text_color="#7ee787")
                for fid in changes:          # 保存後は現在値=保存値に更新
                    self._widgets[fid][2] = changes[fid]
        self.worker.submit(lambda: self.save_fn(changes, restart), done)
