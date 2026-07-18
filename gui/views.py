"""各画面の中身(ttk.Frame)。ウィンドウとは切り離してある。

こうしておくと同じ実装を
  - 新しい1画面シェル(gui/shell.py の左ツリーで切り替え)
  - 既存のゲーム別exe(main_ark.py 等)
の両方に埋め込める(=共通化を保ったまま、両方の形を提供できる)。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .theme import C, ToolBar

# ---------------------------------------------------------------- 共通の小物


def card(parent, **kw) -> ttk.Frame:
    f = ttk.Frame(parent, style="Card.TFrame", **kw)
    return f


def hint(parent, text: str) -> ttk.Label:
    lb = ttk.Label(parent, text=text, style="Muted.TLabel", justify=tk.LEFT)
    lb.pack(anchor=tk.W, padx=4, pady=(0, 8))
    return lb


def make_tree(parent, columns, headings, first_head="名前", first_width=240,
              height=10):
    """列定義からTreeview+スクロールバー。全画面で同じ見た目にする。"""
    wrap = ttk.Frame(parent, style="Card.TFrame")
    wrap.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(wrap, columns=columns, show="tree headings", height=height,
                        selectmode="browse")
    tree.heading("#0", text=first_head)
    tree.column("#0", width=first_width, minwidth=160)
    for c in columns:
        text, width = headings[c]
        tree.heading(c, text=text)
        tree.column(c, width=width, minwidth=60, anchor=tk.CENTER)
    tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(2, 0), pady=2)
    sb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
    sb.pack(side=tk.RIGHT, fill=tk.Y, pady=2)
    tree.configure(yscrollcommand=sb.set)
    from .theme import TAGS
    for tag, color in TAGS.items():
        tree.tag_configure(tag, foreground=color)
    return tree


def fill_tree(tree: ttk.Treeview, rows: list[tuple]) -> None:
    """選択を保ったまま描き直す。rows=[(iid, text, values, tags), ...]"""
    sel = tree.selection()
    tree.delete(*tree.get_children())
    for iid, text, values, tags in rows:
        tree.insert("", tk.END, iid=iid, text=text, values=values, tags=tags)
    for s in sel:
        if tree.exists(s):
            tree.selection_set(s)


def selected_row(parent, tree, rows: list[dict], key: str, what: str) -> dict | None:
    sel = tree.selection()
    if not sel:
        messagebox.showinfo("選択なし", f"{what}を選んでください", parent=parent)
        return None
    return next((r for r in rows if str(r[key]) == sel[0]), None)


def confirm(parent, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, icon="warning", default="no",
                               parent=parent)


def show_error(parent, exc: Exception, title: str = "エラー") -> None:
    from .client import ApiError, ServiceUnavailable
    if isinstance(exc, ServiceUnavailable):
        messagebox.showerror("サービス未接続", str(exc), parent=parent)
    elif isinstance(exc, ApiError):
        messagebox.showerror(title, exc.message, parent=parent)
    else:
        messagebox.showerror(title, str(exc), parent=parent)


class Console(ttk.Frame):
    """RCON入力欄+出力欄。"""

    def __init__(self, master, worker, send_fn, label="RCON:", height=7):
        super().__init__(master)
        self.worker = worker
        self.send_fn = send_fn
        row = ttk.Frame(self)
        row.pack(fill=tk.X, pady=(6, 4))
        ttk.Label(row, text=label, style="Muted.TLabel").pack(side=tk.LEFT, padx=(2, 6))
        self.var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.var)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True)
        e.bind("<Return>", lambda _e: self.send())
        ttk.Button(row, text="送信", command=self.send,
                   style="Accent.TButton").pack(side=tk.LEFT, padx=(6, 2))
        self.out = tk.Text(self, height=height, bg=C["console"], fg=C["text"],
                           insertbackground=C["text"], wrap=tk.WORD, relief="flat",
                           borderwidth=0, padx=8, pady=6)
        self.out.pack(fill=tk.BOTH, expand=True)

    def send(self) -> None:
        cmd = self.var.get().strip()
        if not cmd:
            return
        fn = self.send_fn(cmd)
        if fn is None:
            return

        def done(resp, error):
            if error:
                show_error(self, error, "RCON")
            else:
                self.log(f"> {cmd}\n{resp}")
                self.var.set("")
        self.worker.submit(fn, done)

    def log(self, text: str) -> None:
        self.out.insert(tk.END, text.rstrip() + "\n")
        self.out.see(tk.END)


class BaseView(ttk.Frame):
    """全ビュー共通の土台。client/worker を持ち、定期更新と操作実行を提供する。"""

    title = "画面"

    def __init__(self, master, client, worker):
        super().__init__(master)
        self.client = client
        self.worker = worker
        self._alive = True
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def head(self, text: str, sub: str = "") -> None:
        ttk.Label(self, text=text, style="Title.TLabel").pack(anchor=tk.W, pady=(0, 2))
        if sub:
            hint(self, sub)

    def poll(self, fn, on_ok, interval_ms: int = 5000) -> None:
        def done(result, error):
            if not self._alive or not self.winfo_exists():
                return
            if error is None:
                on_ok(result)
            self.after(interval_ms, lambda: self.poll(fn, on_ok, interval_ms))
        self.worker.submit(fn, done)

    def run_action(self, fn, label: str, log=None) -> None:
        def done(result, error):
            if error:
                show_error(self, error, label)
            elif log:
                log(f"{label}: 受付 (タスク {result.get('task_id')}) "
                    "→ 📋タスク画面で進捗を確認できます")
        self.worker.submit(fn, done)


# ---------------------------------------------------------------- ARK
class ArkView(BaseView):
    title = "🦖 ARK"
    COLS = ("status", "players", "uptime", "build")
    HEADS = {"status": ("状態", 100), "players": ("人数", 70),
             "uptime": ("稼働時間", 120), "build": ("ビルド", 110)}

    def build(self) -> None:
        self._rows: list[dict] = []
        self.head("🦖 ARK サーバー",
                  "行を選んで操作します。状態は常駐サービスが監視しているので即表示されます。")
        self.tree = make_tree(self, self.COLS, self.HEADS, "サーバー", 280, height=11)
        tb = ToolBar(self)
        tb.pack(fill=tk.X, pady=(8, 0))
        tb.add("▶ 起動", self._start, "Accent.TButton")
        tb.add("■ 停止", self._stop, "Danger.TButton")
        tb.add("🔁 再起動", self._restart)
        tb.add("💾 バックアップ", self._backup)
        tb.add("⬆ 更新", self._update)
        tb.add("🧬 プレイヤーデータBK", self._players_backup)
        self.console = Console(self, self.worker, self._rcon_fn)
        self.console.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.poll(self.client.ark, self._fill)

    def _fill(self, rows) -> None:
        self._rows = rows
        out = []
        for a in rows:
            r = a.get("running")
            st = "🟢 稼働中" if r else ("⚪ 停止中" if r is not None else "…")
            name = a["display_name"] + (f"   :{a['game_port']}" if a.get("game_port") else "")
            out.append((str(a["index"]), name,
                        (st, a.get("player_count") if r else "-",
                         a.get("uptime_text", "―"), a.get("build") or "―"),
                        ("active" if r else "off",)))
        fill_tree(self.tree, out)

    def _sel(self):
        return selected_row(self, self.tree, self._rows, "index", "対象のマップ")

    def _start(self):
        a = self._sel()
        if a:
            self.run_action(lambda: self.client.ark_start(a["index"]),
                            f"起動 {a['display_name']}", self.console.log)

    def _stop(self):
        a = self._sel()
        if a and confirm(self, "確認", f"{a['display_name']} を停止しますか?\n"
                                       "(プレイヤーが居れば60/30/10秒前に予告します)"):
            self.run_action(lambda: self.client.ark_stop(a["index"]),
                            f"停止 {a['display_name']}", self.console.log)

    def _restart(self):
        a = self._sel()
        if a and confirm(self, "確認", f"{a['display_name']} を再起動しますか?\n"
                                       "(プレイヤーが居れば60/30/10秒前に予告します)"):
            self.run_action(lambda: self.client.ark_restart(a["index"]),
                            f"再起動 {a['display_name']}", self.console.log)

    def _backup(self):
        a = self._sel()
        if a:
            self.run_action(lambda: self.client.ark_backup(a["index"]),
                            f"バックアップ {a['display_name']}", self.console.log)

    def _update(self):
        a = self._sel()
        if a and messagebox.askyesno(
                "確認", f"{a['display_name']} を更新しますか?\n\n"
                        "更新がある場合のみ 停止(予告あり)→更新→元が稼働中なら起動。\n"
                        "更新が無ければ何もしません。", parent=self):
            self.run_action(lambda: self.client.ark_update(a["index"]),
                            f"更新 {a['display_name']}", self.console.log)

    def _players_backup(self):
        self.run_action(self.client.ark_players_backup, "プレイヤーデータBK",
                        self.console.log)

    def _rcon_fn(self, cmd):
        a = self._sel()
        return (lambda: self.client.ark_rcon(a["index"], cmd)) if a else None


# ---------------------------------------------------------------- MC / Palworld
class ServerView(BaseView):
    """VM上のサーバー(MC/Palworld)。game で絞り込む。"""
    COLS = ("status", "vm", "address")
    HEADS = {"status": ("状態", 100), "vm": ("VM", 120), "address": ("アドレス", 240)}

    def __init__(self, master, client, worker, game: str | None = None,
                 title: str | None = None):
        self.game = game
        self._title = title or "サーバー"
        super().__init__(master, client, worker)

    def build(self) -> None:
        self._rows: list[dict] = []
        self.head(self._title, "行を選んで操作します。状態は常駐サービスが監視しています。")
        self.tree = make_tree(self, self.COLS, self.HEADS, "サーバー", 260, height=9)
        tb = ToolBar(self)
        tb.pack(fill=tk.X, pady=(8, 0))
        tb.add("▶ 起動", lambda: self._act("start"), "Accent.TButton")
        tb.add("■ 停止", lambda: self._act("stop"), "Danger.TButton")
        tb.add("🔁 再起動", lambda: self._act("restart"))
        self.console = Console(self, self.worker, self._rcon_fn)
        self.console.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.poll(self.client.servers, self._fill)

    def _fill(self, servers) -> None:
        self._rows = [s for s in servers if self.game is None or s["game"] == self.game]
        marks = {"active": "🟢 稼働中", "inactive": "⚪ 停止中", "error": "⚠ エラー"}
        fill_tree(self.tree, [
            (s["name"], s["display_name"],
             (marks.get(s.get("status"), str(s.get("status") or "…")),
              s.get("vm") or "-", s.get("fqdn") or s.get("address") or ""),
             ("active" if s.get("status") == "active"
              else "err" if s.get("status") == "error" else "off",))
            for s in self._rows])

    def _sel(self):
        return selected_row(self, self.tree, self._rows, "name", "サーバー")

    def _act(self, act: str) -> None:
        s = self._sel()
        if not s:
            return
        jp = {"start": "起動", "stop": "停止", "restart": "再起動"}[act]
        if act in ("stop", "restart") and not confirm(
                self, "確認", f"{s['display_name']} を{jp}しますか?"):
            return
        self.run_action(lambda: self.client.server_action(s["name"], act),
                        f"{jp} {s['display_name']}", self.console.log)

    def _rcon_fn(self, cmd):
        s = self._sel()
        return (lambda: self.client.server_rcon(s["name"], cmd)) if s else None


# ---------------------------------------------------------------- VM
class VmView(BaseView):
    title = "🖥 VM"
    COLS = ("state", "mem", "servers")
    HEADS = {"state": ("状態", 110), "mem": ("メモリ", 100),
             "servers": ("上で動くサーバー", 320)}

    def build(self) -> None:
        self._vms: list[dict] = []
        self.head("🖥 仮想マシン (Hyper-V)",
                  "VM停止時は、上で動くゲームサーバーを先に安全停止(ワールド保存)します。")
        self.tree = make_tree(self, self.COLS, self.HEADS, "VM", 200, height=11)
        tb = ToolBar(self)
        tb.pack(fill=tk.X, pady=(8, 0))
        tb.add("▶ VM起動", self._start, "Accent.TButton")
        tb.add("■ VM停止(安全)", lambda: self._stop(False), "Danger.TButton")
        tb.add("⏹ VM強制停止", lambda: self._stop(True), "Danger.TButton")
        self.log_out = tk.Text(self, height=6, bg=C["console"], fg=C["text"],
                               wrap=tk.WORD, relief="flat", padx=8, pady=6)
        self.log_out.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.poll(self.client.vms, self._fill, interval_ms=8000)

    def _log(self, t: str) -> None:
        self.log_out.insert(tk.END, t.rstrip() + "\n")
        self.log_out.see(tk.END)

    def _fill(self, vms) -> None:
        self._vms = vms
        fill_tree(self.tree, [
            (v["name"], v["name"],
             ("🟢 Running" if v["state"] == "Running" else f"⚪ {v['state']}",
              f"{v['memory_mb']:,} MB" if v["memory_mb"] else "-",
              ", ".join(v.get("servers") or []) or "-"),
             ("active" if v["state"] == "Running" else "off",))
            for v in vms])

    def _sel(self):
        return selected_row(self, self.tree, self._vms, "name", "VM")

    def _start(self):
        v = self._sel()
        if v:
            self.run_action(lambda: self.client.vm_start(v["name"]),
                            f"VM起動 {v['name']}", self._log)

    def _stop(self, force: bool):
        v = self._sel()
        if not v:
            return
        on = ", ".join(v.get("servers") or []) or "なし"
        if not confirm(self, "確認",
                       f"VM {v['name']} を{'強制' if force else ''}停止しますか?\n\n"
                       f"このVM上のサーバー: {on}\n"
                       "先にゲームサーバーを安全停止(ワールド保存)してからVMを止めます。"):
            return
        self.run_action(lambda: self.client.vm_stop(v["name"], force=force),
                        f"VM{'強制' if force else ''}停止 {v['name']}", self._log)


# ---------------------------------------------------------------- タスク
class TaskView(BaseView):
    title = "📋 タスク"
    COLS = ("status", "category", "lane", "started", "dur")
    HEADS = {"status": ("結果", 80), "category": ("種別", 110), "lane": ("レーン", 130),
             "started": ("開始", 80), "dur": ("所要", 80)}

    def build(self) -> None:
        self.head("📋 タスク",
                  "常駐サービスが持つ実行履歴。画面を閉じても記録は残ります。"
                  "行をクリックで実行ステップを表示。")
        self.tree = make_tree(self, self.COLS, self.HEADS, "操作", 280, height=10)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.detail = tk.Text(self, height=11, bg=C["console"], fg=C["text"],
                              wrap=tk.WORD, relief="flat", padx=8, pady=6)
        self.detail.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        tb = ToolBar(self)
        tb.pack(fill=tk.X, pady=(6, 0))
        tb.add("🧹 履歴をクリア", lambda: self.worker.submit(self.client.tasks_clear))
        self.poll(lambda: self.client.tasks(limit=60), self._fill, interval_ms=3000)

    def _fill(self, tasks) -> None:
        marks = {"success": "✅ 成功", "failed": "❌ 失敗",
                 "running": "⏳ 実行中", "pending": "… 待機"}
        fill_tree(self.tree, [
            (t["id"], t["title"],
             (marks.get(t["status"], t["status"]), t["category"], t["lane"],
              t["started"] or "", f"{t['duration']:.1f}秒" if t["duration"] else ""),
             (t["status"],)) for t in tasks])

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return

        def done(t, error):
            if error:
                return
            self.detail.delete("1.0", tk.END)
            self.detail.insert(tk.END,
                               f"■ {t['title']}\n種別: {t['category']} / "
                               f"レーン: {t['lane']}\n状態: {t['status']}"
                               + (f" / 所要: {t['duration']:.1f}秒"
                                  if t["duration"] else "") + "\n")
            if t.get("error"):
                self.detail.insert(tk.END, f"エラー: {t['error']}\n")
            self.detail.insert(tk.END, "\n── 実行ステップ ──\n" + "\n".join(t["log"]))
        self.worker.submit(lambda: self.client.task(sel[0]), done)
