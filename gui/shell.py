"""1画面シェル。左のツリーで切り替えるモダンGUI。

  ゲームサーバー
    🦖 ARK
    🐑 Palworld
    🟩 Minecraft
  システム
    🖥 VM / ⏰ 予約 / 📋 タスク

中身(views.py)は既存のゲーム別exeと同じものを使い回すので、実装は二重にならない。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from .client import Client
from .sched_view import SchedView
from .theme import C, apply
from .views import ArkView, ServerView, TaskView, VmView

DEFAULT_BASE = "http://127.0.0.1:8770"


class Worker:
    """GUIを固まらせないためのワーカー(APIは全部ここ経由)。"""

    def __init__(self, root: tk.Misc):
        self.root = root
        self._jobs: queue.Queue = queue.Queue()
        self._out: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name="gui-worker").start()
        self.root.after(80, self._poll)

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
        self.root.after(80, self._poll)


class Shell(tk.Tk):
    def __init__(self, base: str = DEFAULT_BASE):
        super().__init__()
        self.title("Game Server Manager")
        self.geometry("1240x780")
        self.minsize(1060, 640)
        self.f = apply(self)
        self.client = Client(base)
        self.worker = Worker(self)
        self._views: dict[str, ttk.Frame] = {}
        self._current: ttk.Frame | None = None

        self._build_header()
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        self._build_sidebar(body)
        self.content = ttk.Frame(body, padding=(16, 14))
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._select("ark")
        self._health_tick()
        self._res_tick()

    # ---------------- ヘッダ ----------------
    def _build_header(self) -> None:
        h = ttk.Frame(self, style="Header.TFrame", padding=(14, 9))
        h.pack(fill=tk.X)
        ttk.Label(h, text="●", style="Header.TLabel",
                  foreground=C["accent"], font=self.f["title"]).pack(side=tk.LEFT)
        ttk.Label(h, text=" Game Server Manager", style="Header.TLabel",
                  font=self.f["bold"]).pack(side=tk.LEFT)
        self.res_var = tk.StringVar(value="")
        ttk.Label(h, textvariable=self.res_var, style="Header.TLabel",
                  foreground=C["muted"], font=self.f["small"]).pack(side=tk.RIGHT)
        self.svc_var = tk.StringVar(value="サービス確認中…")
        self.svc_lb = ttk.Label(h, textvariable=self.svc_var, style="Header.TLabel",
                                font=self.f["small"])
        self.svc_lb.pack(side=tk.RIGHT, padx=16)

    # ---------------- 左ナビ ----------------
    def _build_sidebar(self, parent) -> None:
        side = ttk.Frame(parent, style="Sidebar.TFrame", width=210)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        self.nav = tk.Listbox(
            side, bg=C["sidebar"], fg=C["text"], font=self.f["nav"],
            selectbackground=C["accent_dk"], selectforeground="#ffffff",
            highlightthickness=0, borderwidth=0, activestyle="none")
        self.nav.pack(fill=tk.BOTH, expand=True, padx=6, pady=8)
        # (表示, キー) キーがNone=見出し(選択不可)
        self._nav_items = [
            ("ゲームサーバー", None),
            ("   🦖  ARK", "ark"),
            ("   🐑  Palworld", "pal"),
            ("   🟩  Minecraft", "mc"),
            ("", None),
            ("システム", None),
            ("   🖥  VM", "vm"),
            ("   ⏰  予約", "sched"),
            ("   📋  タスク", "task"),
        ]
        for label, key in self._nav_items:
            self.nav.insert(tk.END, label)
            if key is None and label:
                self.nav.itemconfig(tk.END, foreground=C["muted"])
        self.nav.bind("<<ListboxSelect>>", self._on_nav)

    def _on_nav(self, _e=None) -> None:
        sel = self.nav.curselection()
        if not sel:
            return
        key = self._nav_items[sel[0]][1]
        if key is None:                    # 見出しは選択させない
            self._restore_nav()
            return
        self._select(key)

    def _restore_nav(self) -> None:
        self.nav.selection_clear(0, tk.END)
        for i, (_l, k) in enumerate(self._nav_items):
            if k == self._current_key:
                self.nav.selection_set(i)
                break

    def _select(self, key: str) -> None:
        self._current_key = key
        if key not in self._views:
            self._views[key] = self._make_view(key)
        if self._current is not None:
            self._current.pack_forget()
        self._current = self._views[key]
        self._current.pack(fill=tk.BOTH, expand=True)
        self._restore_nav()

    def _make_view(self, key: str) -> ttk.Frame:
        if key == "ark":
            return ArkView(self.content, self.client, self.worker)
        if key == "pal":
            return ServerView(self.content, self.client, self.worker,
                              game="palworld", title="🐑 Palworld")
        if key == "mc":
            return ServerView(self.content, self.client, self.worker,
                              game="minecraft", title="🟩 Minecraft")
        if key == "vm":
            return VmView(self.content, self.client, self.worker)
        if key == "sched":
            return SchedView(self.content, self.client, self.worker)
        return TaskView(self.content, self.client, self.worker)

    # ---------------- ヘッダの状態 ----------------
    def _health_tick(self) -> None:
        def done(r, error):
            if error is None:
                busy = r.get("busy_lanes") or []
                self.svc_var.set(
                    f"🟢 サービス接続中   ARK {r['ark_maps']} / サーバー {r['servers']}"
                    + (f"   実行中: {len(busy)}件" if busy else ""))
                self.svc_lb.configure(foreground=C["ok"])
            else:
                self.svc_var.set("🔴 サービス未接続 (main_service.py を起動してください)")
                self.svc_lb.configure(foreground=C["err"])
            self.after(4000, self._health_tick)
        self.worker.submit(self.client.health, done)

    def _res_tick(self) -> None:
        """ホストのCPU/メモリを軽く表示(取れなければ黙って隠す)。"""
        def job():
            from core.transport import LocalPowerShell
            r = LocalPowerShell().run_ps(
                "$c=(Get-CimInstance Win32_Processor|Measure-Object -Property "
                "LoadPercentage -Average).Average; "
                "$o=Get-CimInstance Win32_OperatingSystem; "
                "\"$c|$($o.FreePhysicalMemory)|$($o.TotalVisibleMemorySize)\"",
                timeout=15)
            return (r.stdout or "").strip()

        def done(out, error):
            if error is None and out and "|" in out:
                try:
                    c, free, total = out.split("|")
                    used = (int(total) - int(free)) / 1024 / 1024
                    tot = int(total) / 1024 / 1024
                    self.res_var.set(f"CPU {int(float(c or 0))}%   "
                                     f"メモリ {used:.1f}/{tot:.1f} GB")
                except (ValueError, ZeroDivisionError):
                    pass
            self.after(5000, self._res_tick)
        self.worker.submit(job, done)


def run(base: str = DEFAULT_BASE) -> None:
    app = Shell(base)
    if not app.client.alive():
        messagebox.showwarning(
            "GSMサービスに接続できません",
            "常駐サービス(main_service.py)が動いていないようです。\n\n"
            "サービスが動いていないと、予約・バックアップ・動的設定配信も止まります。\n\n"
            f"接続先: {app.client.base}", parent=app)
    app.mainloop()
