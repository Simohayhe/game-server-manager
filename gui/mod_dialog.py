"""Minecraft Mod管理ダイアログ(MC画面内で完結)。

導入済み一覧(削除・更新確認)+ Modrinth/CurseForge 検索→依存込みで導入。
MCバージョンは導入済み fabric-api のバージョンから自動推定(手入力で上書き可)。
"""
from __future__ import annotations

import re
from tkinter import messagebox, ttk

import customtkinter as ctk

from .widgets import ACCENT, CARD, MUTED, TEXT


def _guess_mcver(mods: list[dict]) -> str:
    """導入済みmodから対象MCバージョンを推定(fabric-api の '…+26.1.2' 等)。"""
    for m in mods:
        if m.get("id") in ("fabric-api", "fabricloader", "fabric"):
            ver = m.get("version", "")
            mm = re.search(r"\+(\d+\.\d+(?:\.\d+)?)", ver)
            if mm:
                return mm.group(1)
    for m in mods:                        # 予備: どれかのバージョンから x.y.z を拾う
        mm = re.search(r"(\d+\.\d+\.\d+)", m.get("version", ""))
        if mm:
            return mm.group(1)
    return ""


class ModManager(ctk.CTkToplevel):
    def __init__(self, master, server: dict, client, worker):
        super().__init__(master)
        self.title(f"Mod管理 — {server['display_name']}")
        self.geometry("720x620")
        self.configure(fg_color="#0f1115")
        self.name = server["name"]
        self.client = client
        self.worker = worker
        self._installed: list[dict] = []
        self._results: list[dict] = []

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text=f"🧩 {server['display_name']} の Mod", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text="MC版:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(12, 4))
        self.mcver = ctk.CTkEntry(head, width=90, height=28,
                                  placeholder_text="26.1.2")
        self.mcver.pack(side="left")

        self.tabview = ctk.CTkTabview(self, fg_color=CARD)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=8)
        self.tabview.add("導入済み")
        self.tabview.add("追加(検索)")
        self._build_installed(self.tabview.tab("導入済み"))
        self._build_search(self.tabview.tab("追加(検索)"))

        self.status = ctk.CTkLabel(self, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=14, pady=(0, 10))
        self.after(120, self.lift)
        self._load_installed()

    # ---- 導入済みタブ ----
    def _build_installed(self, parent):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=4, pady=4)
        self.itree = ttk.Treeview(wrap, columns=("ver", "upd"), show="tree headings",
                                  selectmode="extended", style="D.Treeview", height=12)
        self.itree.heading("#0", text="Mod")
        self.itree.column("#0", width=320)
        self.itree.heading("ver", text="バージョン")
        self.itree.column("ver", width=180, anchor="center")
        self.itree.heading("upd", text="更新")
        self.itree.column("upd", width=120, anchor="center")
        self.itree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.itree.yview,
                           style="D.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.itree.configure(yscrollcommand=sb.set)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(6, 4))
        ctk.CTkButton(bar, text="🔄 再読込", height=30, width=90, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._load_installed).pack(side="left")
        ctk.CTkButton(bar, text="🔍 更新を確認", height=30, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self._check_updates).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="🗑 選択を削除", height=30, corner_radius=6,
                      fg_color="#3a2226", hover_color="#4d2a30",
                      command=self._remove).pack(side="right")

    def _load_installed(self):
        self.status.configure(text="導入済みmodを取得中…")

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"取得失敗: {err}")
                return
            self._installed = res or []
            self.itree.delete(*self.itree.get_children())
            for m in self._installed:
                self.itree.insert("", "end", iid=m["file"],
                                  text=m.get("name") or m["file"],
                                  values=(m.get("version", ""), ""))
            if not self.mcver.get().strip():
                self.mcver.insert(0, _guess_mcver(self._installed))
            self.status.configure(text=f"{len(self._installed)}個のmod")
        self.worker.submit(lambda: self.client.mods_list(self.name), done)

    def _check_updates(self):
        mcver = self.mcver.get().strip()
        if not mcver:
            messagebox.showinfo("MC版が必要", "MCバージョンを入力してください", parent=self)
            return
        self.status.configure(text="更新を確認中(Modrinth照会)…")

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"更新確認に失敗: {err}")
                return
            by_file = {u["file"]: u for u in (res or [])}
            for m in self._installed:
                u = by_file.get(m["file"])
                if not u:
                    continue
                mark = ("🆕 " + u["latest"]) if u.get("update") else (
                    "✅ 最新" if u.get("source") == "modrinth" else "─")
                self.itree.set(m["file"], "upd", mark)
            self.status.configure(text="更新確認 完了(🆕=更新あり / ─=判定不可)")
        self.worker.submit(lambda: self.client.mods_check_updates(self.name, mcver), done)

    def _remove(self):
        sel = self.itree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "削除するmodを選んでください", parent=self)
            return
        if not messagebox.askyesno("削除の確認",
                f"{len(sel)}個のmodを削除して再起動します。よろしいですか?",
                icon="warning", default="no", parent=self):
            return

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("削除", str(err), parent=self)
            else:
                self.status.configure(text="削除を開始しました(📋タスクで進捗)")
                self.after(6000, self._load_installed)
        self.worker.submit(lambda: self.client.mods_remove(self.name, list(sel)), done)

    # ---- 検索タブ ----
    def _build_search(self, parent):
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=4, pady=4)
        self.q = ctk.CTkEntry(top, placeholder_text="mod名で検索…", height=30)
        self.q.pack(side="left", fill="x", expand=True)
        self.q.bind("<Return>", lambda _e: self._search())
        self.source = ctk.CTkOptionMenu(top, values=["modrinth", "curseforge"],
                                        width=110, height=30,
                                        font=ctk.CTkFont(size=11))
        self.source.pack(side="left", padx=6)
        ctk.CTkButton(top, text="検索", width=70, height=30, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._search).pack(side="left")
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=4, pady=4)
        self.stree = ttk.Treeview(wrap, columns=("dl",), show="tree headings",
                                  selectmode="browse", style="D.Treeview", height=11)
        self.stree.heading("#0", text="Mod")
        self.stree.column("#0", width=430)
        self.stree.heading("dl", text="DL数")
        self.stree.column("dl", width=110, anchor="center")
        self.stree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.stree.yview,
                           style="D.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.stree.configure(yscrollcommand=sb.set)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(6, 4))
        ctk.CTkButton(bar, text="⬇ 選択を導入(依存も自動)", height=30, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._install).pack(side="right")

    def _search(self):
        q = self.q.get().strip()
        mcver = self.mcver.get().strip()
        if not q or not mcver:
            messagebox.showinfo("入力不足", "MC版と検索語を入力してください", parent=self)
            return
        self.status.configure(text="検索中…")

        def done(res, err):
            if not self.winfo_exists():
                return
            if err:
                self.status.configure(text=f"検索失敗: {err}")
                return
            self._results = res or []
            self.stree.delete(*self.stree.get_children())
            for i, m in enumerate(self._results):
                dl = m.get("downloads")
                self.stree.insert("", "end", iid=str(i),
                                  text=f"{m.get('name')} — {m.get('description','')[:60]}",
                                  values=(f"{dl:,}" if isinstance(dl, int) else "",))
            self.status.configure(text=f"{len(self._results)}件ヒット")
        self.worker.submit(
            lambda: self.client.mods_search(self.name, q, mcver, self.source.get()), done)

    def _install(self):
        sel = self.stree.selection()
        if not sel:
            messagebox.showinfo("選択なし", "導入するmodを選んでください", parent=self)
            return
        m = self._results[int(sel[0])]
        mcver = self.mcver.get().strip()
        if not messagebox.askyesno("導入の確認",
                f"「{m.get('name')}」を必須依存ごと導入して再起動します。よろしいですか?",
                parent=self):
            return
        self.status.configure(text=f"「{m.get('name')}」を導入中(📋タスクで進捗)…")

        def done(_r, err):
            if not self.winfo_exists():
                return
            if err:
                messagebox.showerror("導入", str(err), parent=self)
            else:
                self.after(8000, self._load_installed)
        self.worker.submit(
            lambda: self.client.mods_install(self.name, self.source.get(),
                                             m.get("id"), mcver), done)
