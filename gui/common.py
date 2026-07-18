"""各GUI(ARK/MC/Palworld/予約)で共有する土台と部品。

旧 gui/app.py は6,300行の一枚岩だった。ゲーム別exeに分けた際、今度は各GUIに
ウィンドウ初期化・タスク画面・RCONコンソール・ツリー更新をコピペしてしまったので、
共通化できるものは全部ここへ集約する。各GUIは「自分の画面」だけを書けばよい。

  BaseApp     … ウィンドウ+スタイル+APIクライアント+ワーカー+接続バー+タスクタブ
  TaskTab     … タスク一覧+詳細(サービスが持つ履歴を表示)
  RconConsole … RCON入力欄+出力欄
  fill_tree   … 選択を保ったままツリーを描き直す
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from .client import ApiError, Client, ServiceUnavailable

# 旧GUIと同じ配色(見た目を変えずに分割・共通化するため)
PAL = {
    "bg": "#f5f6f8", "panel": "#ffffff", "text": "#1f2430", "muted": "#6b7280",
    "accent": "#3b82f6", "ok": "#16a34a", "warn": "#d97706", "err": "#dc2626",
    "dark_bg": "#11151c", "dark_fg": "#d7dce5",
}
TAG_COLORS = {"active": PAL["ok"], "off": PAL["muted"], "err": PAL["err"]}
DEFAULT_BASE = "http://127.0.0.1:8770"


def apply_style(root: tk.Misc) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=PAL["bg"], foreground=PAL["text"])
    style.configure("TFrame", background=PAL["bg"])
    style.configure("TLabel", background=PAL["bg"], foreground=PAL["text"])
    style.configure("TButton", padding=(8, 4))
    style.configure("Treeview", background=PAL["panel"], fieldbackground=PAL["panel"],
                    foreground=PAL["text"], rowheight=24)
    style.configure("Treeview.Heading", background="#e8eaee", foreground=PAL["text"])
    style.map("Treeview", background=[("selected", PAL["accent"])],
              foreground=[("selected", "#ffffff")])


def hint(parent, text: str):
    """画面上部の淡色ヒント行。"""
    lb = ttk.Label(parent, foreground=PAL["muted"], justify=tk.LEFT, text=text)
    lb.pack(anchor=tk.W, padx=8, pady=(6, 2))
    return lb


def make_tree(parent, columns, headings, first_head="名前", first_width=240,
              height=10, **kw):
    """列定義からTreeview+スクロールバーを作る(全GUIで同じ見た目にする)。

    columns  : ("status", "players", ...)
    headings : {"status": ("状態", 90), ...}  → (見出し, 幅)
    """
    frame = ttk.Frame(parent)
    frame.pack(fill=tk.BOTH, expand=True, padx=8)
    tree = ttk.Treeview(frame, columns=columns, show="tree headings", height=height,
                        selectmode=kw.pop("selectmode", "browse"), **kw)
    tree.heading("#0", text=first_head)
    tree.column("#0", width=first_width)
    for c in columns:
        text, width = headings[c]
        tree.heading(c, text=text)
        tree.column(c, width=width, anchor=kw.get("anchor", tk.CENTER))
    tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
    sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    tree.configure(yscrollcommand=sb.set)
    for tag, color in TAG_COLORS.items():
        tree.tag_configure(tag, foreground=color)
    return tree


def selected_row(parent, tree: ttk.Treeview, rows: list[dict], key: str,
                 what: str = "行") -> dict | None:
    """ツリーの選択行に対応する辞書を返す。未選択なら案内を出して None。

    key は行を一意にしている項目名(ARK=index / サーバー=name / 予約=id)。
    iidは文字列なので str() で突き合わせる。
    """
    sel = tree.selection()
    if not sel:
        messagebox.showinfo("選択なし", f"{what}を選んでください", parent=parent)
        return None
    return next((r for r in rows if str(r[key]) == sel[0]), None)


def fill_tree(tree: ttk.Treeview, rows: list[tuple]) -> None:
    """選択を保ったままツリーを描き直す。rows=[(iid, text, values, tags), ...]

    毎回delete→insertすると選択が飛んで操作しづらいので、全GUIでこれを使う。
    """
    sel = tree.selection()
    tree.delete(*tree.get_children())
    for iid, text, values, tags in rows:
        tree.insert("", tk.END, iid=iid, text=text, values=values, tags=tags)
    for s in sel:
        if tree.exists(s):
            tree.selection_set(s)


class Worker:
    """GUIを固まらせないための最小ワーカー。

    重い処理(API呼び出し)はここで実行し、結果はキュー経由でUIスレッドへ返す
    (tkinterのウィジェット操作はUIスレッド限定のため)。
    実処理はサービス側にあるので、GUIは「APIを叩いて待つ」だけでよい。
    """

    def __init__(self, root: tk.Misc):
        self.root = root
        self._jobs: queue.Queue = queue.Queue()
        self._out: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="gui-worker").start()
        self.root.after(100, self._poll)

    def submit(self, fn, on_done=None) -> None:
        self._jobs.put((fn, on_done))

    def _loop(self) -> None:
        while True:
            fn, on_done = self._jobs.get()
            try:
                self._out.put((on_done, fn(), None))
            except Exception as exc:
                traceback.print_exc()
                self._out.put((on_done, None, exc))

    def _poll(self) -> None:
        try:
            while True:
                on_done, result, error = self._out.get_nowait()
                if on_done:
                    try:
                        on_done(result, error)
                    except Exception:
                        traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


class ServiceBar(ttk.Frame):
    """上部に常時出すサービス接続状態。サービスが落ちていれば赤く出す。"""

    def __init__(self, master, client: Client, worker: Worker):
        super().__init__(master)
        self.client = client
        self.worker = worker
        self.var = tk.StringVar(value="サービス: 確認中…")
        self.label = ttk.Label(self, textvariable=self.var, foreground=PAL["muted"])
        self.label.pack(side=tk.LEFT, padx=8, pady=3)
        self._tick()

    def _tick(self) -> None:
        def done(result, error):
            if error is None:
                busy = result.get("busy_lanes") or []
                self.var.set(
                    f"🟢 サービス接続中  ARK {result['ark_maps']}マップ / "
                    f"サーバー {result['servers']}台"
                    + (f"  実行中: {', '.join(busy)}" if busy else ""))
                self.label.configure(foreground=PAL["ok"])
            else:
                self.var.set("🔴 GSMサービスに接続できません "
                             "(main_service.py が動いているか確認してください)")
                self.label.configure(foreground=PAL["err"])
            self.after(4000, self._tick)
        self.worker.submit(self.client.health, done)


class TaskTab(ttk.Frame):
    """タスク一覧+詳細。サービスが持つ履歴なのでGUIを閉じても残る。"""

    def __init__(self, master, client: Client, worker: Worker):
        super().__init__(master)
        self.client = client
        self.worker = worker
        hint(self, "↳ サービスが持つ実行履歴。この画面を閉じても記録は残ります。"
                   "行をクリックで詳細(実行ステップ)を表示。")
        cols = ("status", "category", "lane", "started", "dur")
        heads = {"status": ("結果", 70), "category": ("種別", 100),
                 "lane": ("レーン", 120), "started": ("開始", 70), "dur": ("所要", 80)}
        self.tree = make_tree(self, cols, heads, first_head="操作", first_width=260,
                              height=9)
        for tag, color in (("success", PAL["ok"]), ("failed", PAL["err"]),
                           ("running", PAL["accent"]), ("pending", PAL["muted"])):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.detail = tk.Text(self, height=10, bg=PAL["dark_bg"], fg=PAL["dark_fg"],
                              wrap=tk.WORD)
        self.detail.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        ttk.Button(self, text="🧹 履歴をクリア", command=self._clear
                   ).pack(anchor=tk.E, padx=8, pady=(0, 6))
        self._refresh()

    def _refresh(self) -> None:
        def done(tasks, error):
            if error is None:
                marks = {"success": "✅ 成功", "failed": "❌ 失敗",
                         "running": "⏳ 実行中", "pending": "… 待機"}
                fill_tree(self.tree, [
                    (t["id"], t["title"],
                     (marks.get(t["status"], t["status"]), t["category"], t["lane"],
                      t["started"] or "",
                      f"{t['duration']:.1f}秒" if t["duration"] else ""),
                     (t["status"],)) for t in tasks])
            self.after(3000, self._refresh)
        self.worker.submit(lambda: self.client.tasks(limit=60), done)

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
                               + (f" / 所要: {t['duration']:.1f}秒" if t["duration"] else "")
                               + "\n")
            if t.get("error"):
                self.detail.insert(tk.END, f"エラー: {t['error']}\n")
            self.detail.insert(tk.END, "\n── 実行ステップ ──\n" + "\n".join(t["log"]))
        self.worker.submit(lambda: self.client.task(sel[0]), done)

    def _clear(self) -> None:
        self.worker.submit(self.client.tasks_clear)


class RconConsole(ttk.Frame):
    """RCON入力欄+出力欄。send_fn(cmd) が実際の送信を担う。"""

    def __init__(self, master, worker: Worker, send_fn, label: str = "RCON:",
                 height: int = 8):
        super().__init__(master)
        self.worker = worker
        self.send_fn = send_fn
        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Label(row, text=label).pack(side=tk.LEFT)
        self.var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.var)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        e.bind("<Return>", lambda _e: self.send())
        ttk.Button(row, text="送信", command=self.send).pack(side=tk.LEFT)
        self.out = tk.Text(self, height=height, bg=PAL["dark_bg"], fg=PAL["dark_fg"],
                           insertbackground=PAL["dark_fg"], wrap=tk.WORD)
        self.out.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    def send(self) -> None:
        cmd = self.var.get().strip()
        if not cmd:
            return

        def done(resp, error):
            if error:
                show_error(self, error, "RCON")
            else:
                self.log(f"> {cmd}\n{resp}")
                self.var.set("")
        result = self.send_fn(cmd)
        if result is None:           # 対象未選択などで送信side が中断した
            return
        self.worker.submit(result, done)

    def log(self, text: str) -> None:
        self.out.insert(tk.END, text.rstrip() + "\n")
        self.out.see(tk.END)


class BaseApp(tk.Tk):
    """全GUI共通の土台。各GUIは build_tabs() で自分のタブを足すだけでよい。"""

    def __init__(self, base: str, title: str, size: str = "1020x700",
                 minsize: tuple[int, int] = (900, 560)):
        super().__init__()
        self.title(title)
        self.geometry(size)
        self.minsize(*minsize)
        apply_style(self)
        self.configure(bg=PAL["bg"])
        self.client = Client(base)
        self.worker = Worker(self)
        ServiceBar(self, self.client, self.worker).pack(fill=tk.X)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.build_tabs()
        self.nb.add(TaskTab(self.nb, self.client, self.worker), text="📋 タスク")
        guard_service(self, self.client)

    def build_tabs(self) -> None:
        """各GUIが自分のタブを self.nb に足す。"""
        raise NotImplementedError

    def add_tab(self, text: str) -> ttk.Frame:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=text)
        return f

    def run_action(self, fn, label: str, log=None) -> None:
        """操作APIを叩いてタスクIDを受け取る(実処理はサービス側で走る)。"""
        def done(result, error):
            if error:
                show_error(self, error, label)
            elif log:
                log(f"{label}: 受付 (タスク {result.get('task_id')}) "
                    "→ 📋タスクタブで進捗を確認できます")
        self.worker.submit(fn, done)

    def poll(self, fn, on_ok, interval_ms: int = 5000) -> None:
        """定期的にAPIを叩いて画面を更新する。"""
        def done(result, error):
            if error is None:
                on_ok(result)
            self.after(interval_ms, lambda: self.poll(fn, on_ok, interval_ms))
        self.worker.submit(fn, done)


def guard_service(root: tk.Misc, client: Client) -> bool:
    """起動時にサービスの生死を確認し、落ちていれば案内する。"""
    if client.alive():
        return True
    messagebox.showwarning(
        "GSMサービスに接続できません",
        "常駐サービス(main_service.py)が動いていないようです。\n\n"
        "サービスが動いていないと、予約・バックアップ・動的設定配信も止まります。\n"
        "サービスを起動してから、この画面を開き直してください。\n\n"
        f"接続先: {client.base}",
        parent=root)
    return False


def show_error(parent, exc: Exception, title: str = "エラー") -> None:
    if isinstance(exc, ServiceUnavailable):
        messagebox.showerror("サービス未接続", str(exc), parent=parent)
    elif isinstance(exc, ApiError):
        messagebox.showerror(title, exc.message, parent=parent)
    else:
        messagebox.showerror(title, str(exc), parent=parent)


def confirm(parent, title: str, message: str) -> bool:
    return messagebox.askyesno(title, message, icon="warning", default="no",
                               parent=parent)
