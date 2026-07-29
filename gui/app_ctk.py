"""customtkinter版シェル。左ナビ + ダッシュボード + ゲーム別画面 + ライブログ。

  ダッシュボード
  ゲームサーバー   🦖 ARK / 🐑 Palworld / 🟩 Minecraft
  システム        🖥 VM / ⏰ 予約 / 📋 タスク

一覧は ttk.Treeview を使う(customtkinterに表ウィジェットが無いため)。
配色はcustomtkinterのダークに合わせてある。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

import customtkinter as ctk

from .client import ApiError, Client, ServiceUnavailable
from .dashboard import Dashboard
from .widgets import ACCENT, CARD, ERR, MUTED, OK, TEXT, LogView

DEFAULT_BASE = "http://127.0.0.1:8770"
APP_VERSION = "3.6.0"                            # リリースtagと比較して更新通知を出す
GITHUB_REPO = "Simohayhe/game-server-manager"    # アップデート確認先
UI_SCALES = {"80%": 0.8, "90%": 0.9, "100%": 1.0, "110%": 1.1, "125%": 1.25}


def _scale_path():
    from core.paths import app_dir
    return app_dir() / "uiscale.json"


def load_scale() -> str:
    """表示サイズの設定を読む(既定100%)。画面ごとに好みが違うので永続化する。"""
    import json
    try:
        v = json.loads(_scale_path().read_text(encoding="utf-8")).get("ui_scale")
        return v if v in UI_SCALES else "100%"
    except Exception:
        return "100%"


def save_scale(label: str) -> None:
    import json
    try:
        _scale_path().write_text(json.dumps({"ui_scale": label}), encoding="utf-8")
    except OSError:
        pass
SIDE = "#171a20"
BG = "#0f1115"


class Worker:
    """UIを固めないための1本のバックグラウンドスレッド。

    poll_ms = 結果をUIへ取り込む間隔。ライブログ用のワーカーは
    ①ロングポーリングで最大数十秒ブロックするので画面更新用と分ける必要があり
    ②取り込み遅延がそのまま表示遅延になるので細かく回す(20ms)。
    """

    def __init__(self, root, poll_ms: int = 80, name: str = "gui-worker"):
        self.root = root
        self.poll_ms = poll_ms
        self._jobs: queue.Queue = queue.Queue()
        self._out: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True, name=name).start()
        self.root.after(poll_ms, self._poll)

    def submit(self, fn, on_done=None):
        self._jobs.put((fn, on_done))

    def _loop(self):
        while True:
            fn, cb = self._jobs.get()
            try:
                self._out.put((cb, fn(), None))
            except Exception as exc:
                traceback.print_exc()
                self._out.put((cb, None, exc))

    def _poll(self):
        try:
            while True:
                cb, res, err = self._out.get_nowait()
                if cb:
                    try:
                        cb(res, err)
                    except Exception:
                        traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(self.poll_ms, self._poll)


def ui_font(root) -> str:
    """読みやすい日本語UIフォントを選ぶ。

    既定の「Yu Gothic UI(游ゴシック)」は線が細く、ダーク背景だと薄く見えて読みにくい
    (実際に「見にくい」と指摘を受けた)。太めでくっきりする Meiryo UI を優先する。
    """
    from tkinter import font as tkfont
    fams = set(tkfont.families(root))
    for f in ("Meiryo UI", "Meiryo", "BIZ UDPGothic", "MS UI Gothic", "Yu Gothic UI"):
        if f in fams:
            return f
    return "TkDefaultFont"


def style_tree(root, scale: float = 1.0) -> None:
    """ttk.Treeview を customtkinter のダークに馴染ませる。

    scale = 表示サイズ倍率。画面によって「小さい」「大きい」の感じ方が変わるので
    (実際に両方の指摘を受けた)、決め打ちにせずヘッダーの切替から変えられるようにした。
    基準(1.0)は 本文11 / 見出し10 / 行高30。
    """
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    f = ui_font(root)
    # フォントは Meiryo UI(游ゴシックは細くてダーク背景で薄い)。本文色は明るめでコントラスト確保。
    body = max(8, round(11 * scale))
    head = max(8, round(10 * scale))
    rowh = max(18, round(30 * scale))
    st.configure("D.Treeview", background=CARD, fieldbackground=CARD,
                 foreground="#f0f3f6", rowheight=rowh, borderwidth=0, font=(f, body))
    st.configure("D.Treeview.Heading", background="#2b303a", foreground="#b9c2cc",
                 relief="flat", padding=(10, 8), borderwidth=0,
                 font=(f, head, "bold"))
    st.map("D.Treeview.Heading", background=[("active", "#343a45")])
    st.map("D.Treeview", background=[("selected", "#2f5c9e")],
           foreground=[("selected", "#ffffff")])
    st.configure("D.Vertical.TScrollbar", background="#2b303a", troughcolor=CARD,
                 arrowcolor=MUTED, borderwidth=0)


def tree(parent, columns, headings, first="名前", first_w=240, height=10,
         selectmode="browse"):
    wrap = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10)
    wrap.pack(fill="both", expand=True)
    t = ttk.Treeview(wrap, columns=columns, show="tree headings", height=height,
                     selectmode=selectmode, style="D.Treeview")
    t.heading("#0", text=first)
    t.column("#0", width=first_w, minwidth=150)
    for c in columns:
        txt, w = headings[c]
        t.heading(c, text=txt)
        t.column(c, width=w, minwidth=60, anchor="center")
    t.pack(fill="both", expand=True, side="left", padx=(6, 0), pady=6)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=t.yview,
                       style="D.Vertical.TScrollbar")
    sb.pack(side="right", fill="y", pady=6, padx=(0, 6))
    t.configure(yscrollcommand=sb.set)
    for tag, col in (("active", OK), ("off", MUTED), ("err", ERR),
                     ("success", OK), ("failed", ERR), ("running", ACCENT),
                     ("pending", MUTED)):
        t.tag_configure(tag, foreground=col)
    return t


def fill(t, rows) -> None:
    sel = t.selection()
    t.delete(*t.get_children())
    for iid, text, values, tags in rows:
        t.insert("", "end", iid=iid, text=text, values=values, tags=tags)
    for s in sel:
        if t.exists(s):
            t.selection_set(s)


def picked(parent, t, rows, key, what):
    sel = t.selection()
    if not sel:
        messagebox.showinfo("選択なし", f"{what}を選んでください", parent=parent)
        return None
    return next((r for r in rows if str(r[key]) == sel[0]), None)


def ask(parent, msg) -> bool:
    return messagebox.askyesno("確認", msg, icon="warning", default="no", parent=parent)


class Page(ctk.CTkFrame):
    """各画面の基底。

    ポーリングは「表示中のページだけ」行う。全ページを一度ずつ開くと、隠れたページも
    ずっと更新し続けて 170回/分 のAPI呼び出しになってしまうため(実測して判明)。
    表示/非表示は App.show() が on_show()/on_hide() で知らせる。
    """

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.client = app.client
        self.worker = app.worker
        self._alive = True
        self._visible = False
        self._poll_gen = 0                 # ポーリング世代(多重実行防止)
        self._polls: list[tuple] = []      # (fn, ok, ms) 表示中だけ回す
        self.build()

    def destroy(self):
        self._alive = False
        super().destroy()

    def build(self):
        raise NotImplementedError

    def title(self, text: str) -> None:
        ctk.CTkLabel(self, text=text, text_color=TEXT,
                     font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w",
                                                                    pady=(0, 10))

    # ---- 表示/非表示 ----
    # ポーリングは「世代(_poll_gen)」で管理する。表示/非表示のたびに世代を進め、
    # 古い世代の連鎖は自然消滅させる。これをしないと、表示→非表示→再表示を
    # ポーリング間隔内に行ったとき、古い連鎖が生き返って多重に走り、画面が
    # 何度も再描画されてチラつく(バックアップ画面で顕著だった)。
    def on_show(self) -> None:
        if self._visible:
            return
        self._visible = True
        self._poll_gen += 1
        gen = self._poll_gen
        for spec in self._polls:
            self._run_poll(spec, gen)
        log = getattr(self, "log", None)
        if log is not None:
            log.start()

    def on_hide(self) -> None:
        self._visible = False
        self._poll_gen += 1            # 走っている連鎖を無効化(古い世代は止まる)
        log = getattr(self, "log", None)
        if log is not None:
            log.stop()                 # ライブログの追尾も止める(3秒毎の無駄を消す)

    def poll(self, fn, ok, ms=5000):
        """表示中だけ回すポーリングを登録する。"""
        spec = (fn, ok, ms)
        self._polls.append(spec)
        if self._visible:
            self._run_poll(spec, self._poll_gen)

    def _run_poll(self, spec, gen) -> None:
        fn, ok, ms = spec

        def done(res, err):
            # 非表示になった/世代が変わった連鎖はここで止める(多重実行を防ぐ)
            if (not self._alive or not self.winfo_exists()
                    or not self._visible or gen != self._poll_gen):
                return
            if err is None:
                ok(res)
            self.after(ms, lambda: (self._run_poll(spec, gen)
                                    if self._visible and gen == self._poll_gen
                                    else None))
        self.worker.submit(fn, done)

    def act(self, fn, label):
        """操作を投げ、裏のタスクが成功/失敗するまで見届けて結果を表示する。"""
        from .jobwait import watch_job, _last_log
        self.app.toast(f"{label}… 実行中")

        def upd(state, task):
            if state == "running":
                line = _last_log(task)
                self.app.toast(f"{label}… {line}" if line else f"{label}… 実行中")
            elif state == "success":
                self.app.toast(f"✅ {label} 成功")
            elif state in ("failed", "error"):
                err = (task or {}).get("error") or "不明なエラー"
                self.app.toast(f"❌ {label} 失敗")
                messagebox.showerror(label, str(err), parent=self)
        watch_job(self, self.worker, fn, self.client.task, upd)

    def bar(self):
        b = ctk.CTkFrame(self, fg_color="transparent")
        b.pack(fill="x", pady=(10, 0))
        return b

    def attach_menu(self, tree, items_fn) -> None:
        """一覧の右クリックメニューを付ける。メニューはすっきり保ち、副次操作はここに集約。

        items_fn() は [(ラベル, コールバック) または None(区切り線)] を返す。
        右クリックした行を選択してからメニューを出す(選択対象に対して操作する)。
        """
        menu = tk.Menu(tree, tearoff=0, bg=CARD, fg="#e6edf3",
                       activebackground="#2f5c9e", activeforeground="#ffffff",
                       bd=0, font=(ui_font(self), 10))

        def popup(event):
            row = tree.identify_row(event.y)
            if row:
                tree.selection_set(row)
            menu.delete(0, "end")
            for item in items_fn():
                if item is None:
                    menu.add_separator()
                else:
                    menu.add_command(label=item[0], command=item[1])
            if menu.index("end") is not None:
                menu.tk_popup(event.x_root, event.y_root)
        tree.bind("<Button-3>", popup)
        # ヒント(一覧の下に淡色で表示)
        ctk.CTkLabel(self, text="↳ 一覧を右クリックで その他の操作",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(4, 0))

    @staticmethod
    def btn(parent, text, cmd, kind="normal"):
        colors = {"normal": ("#2b303a", "#39404d"), "primary": (ACCENT, "#4a86e0"),
                  "danger": ("#3a2226", "#4d2a30")}
        fg, hv = colors[kind]
        return ctk.CTkButton(parent, text=text, command=cmd, fg_color=fg,
                             hover_color=hv, corner_radius=8, height=34,
                             text_color="#ffffff" if kind == "primary" else TEXT,
                             font=ctk.CTkFont(size=12))


def _ark_status_text(a: dict) -> str:
    """ARKの状態表示。プロセスは居るが未 advertising なら『起動中…』。

    ASAはプロセス起動から実際に参加可能(advertising for join)になるまで数十秒あるので、
    その間は『稼働中』ではなく『起動中…』を出す(早すぎる完了表示への対処)。
    """
    if not a.get("running"):
        return "⚪ 停止中" if a.get("running") is not None else "…"
    return "🟢 稼働中" if a.get("ready") else "🟡 起動中…"


def _ark_status_tag(a: dict) -> str:
    if not a.get("running"):
        return "off"
    return "active" if a.get("ready") else "pending"


class ArkPage(Page):
    COLS = ("status", "players", "public", "uptime", "version")
    _log_target = None          # 今ログを表示している対象(切替検知用)
    H = {"status": ("状態", 110), "players": ("人数", 60), "public": ("外部公開", 90),
         "uptime": ("稼働時間", 120), "version": ("バージョン", 110)}

    def build(self):
        self._rows = []
        self.title("🦖 ARK")
        # 複数選択可(Ctrl/Shiftクリック)。一括起動・ローリング更新に使う。
        self.t = tree(self, self.COLS, self.H, "サーバー", 300, 8,
                      selectmode="extended")
        b = self.bar()
        for txt, cmd, kind in (("▶ 起動", self._start, "primary"),
                               ("■ 停止", self._stop, "danger"),
                               ("🔁 再起動", self._restart, "normal"),
                               ("⬆ 更新", self._update, "normal"),
                               ("🧬 プレイヤーBK", self._pbk, "normal"),
                               ("↩ プレイヤー復元", self._prestore, "normal")):
            self.btn(b, txt, cmd, kind).pack(side="left", padx=(0, 6))
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.pack(fill="x", pady=(8, 0))
        self.respawn_sw = ctk.CTkSwitch(
            opts, text="🦕 再起動時に野生恐竜をリスポーン", onvalue=True, offvalue=False,
            command=self._set_respawn, font=ctk.CTkFont(size=12))
        self.respawn_sw.pack(side="left")
        ctk.CTkLabel(opts, text="   🎃 イベント:", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=(12, 2))
        self.event_menu = ctk.CTkOptionMenu(
            opts, values=["なし(通常)"], command=self._set_event,
            width=210, font=ctk.CTkFont(size=12))
        self.event_menu.pack(side="left")
        self._ev_l2v: dict[str, str] = {}   # ラベル→値
        self._ev_v2l: dict[str, str] = {}   # 値→ラベル
        ctk.CTkLabel(opts, text="  (全マップ・次回起動時に反映)",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left")
        self.attach_menu(self.t, self._menu_items)
        self.upd_banner = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w",
                                       font=ctk.CTkFont(size=12))
        self.upd_banner.pack(anchor="w", pady=(8, 0))
        ctk.CTkLabel(self, text="ライブログ", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w",
                                                                    pady=(14, 4))
        # ログ専用ワーカー: ロングポーリングは最大10秒ブロックするので画面更新用と分ける
        self.log = LogView(self, self._log_fn, Worker(self, 20, "ark-log"))
        self.log.pack(fill="both", expand=True)   # 追尾は on_show で開始する
        self.poll(self.client.ark_meta, self._fill)
        self._respawn_on_restart = False
        self.worker.submit(self.client.ark_behavior, self._apply_respawn)  # 現在値を反映
        self.worker.submit(self.client.ark_event, self._apply_event)       # イベント現在値

    def _apply_respawn(self, res, err):
        if err or not self.respawn_sw.winfo_exists():
            return
        self._respawn_on_restart = bool(res.get("respawn_on_restart"))
        (self.respawn_sw.select if self._respawn_on_restart
         else self.respawn_sw.deselect)()

    def _set_respawn(self):
        want = bool(self.respawn_sw.get())

        def done(res, err):
            if err:
                messagebox.showerror("設定", str(err), parent=self)
                return
            self._respawn_on_restart = bool(res.get("respawn_on_restart"))
            self.app.toast(f"再起動時の恐竜リスポーン: "
                           f"{'ON' if self._respawn_on_restart else 'OFF'}")
        self.worker.submit(lambda: self.client.ark_behavior_set(want), done)

    def _apply_event(self, res, err):
        if err or not self.event_menu.winfo_exists():
            return
        choices = res.get("choices") or [{"value": "", "label": "なし(通常)"}]
        self._ev_l2v = {c["label"]: c["value"] for c in choices}
        self._ev_v2l = {c["value"]: c["label"] for c in choices}
        self._event_note = res.get("note", "")
        self.event_menu.configure(values=[c["label"] for c in choices])
        cur = res.get("event", "")
        self.event_menu.set(self._ev_v2l.get(cur, "なし(通常)"))

    def _set_event(self, label):
        value = self._ev_l2v.get(label, "")

        def done(res, err):
            if err:
                messagebox.showerror("イベント", str(err), parent=self)
                self.worker.submit(self.client.ark_event, self._apply_event)  # 元に戻す
                return
            ev = res.get("event", "")
            shown = self._ev_v2l.get(ev, "なし(通常)")
            self.app.toast(f"ARKイベントを「{shown}」に設定(次回起動時に全マップへ反映)")
            if ev:            # イベントを有効化したときは反映条件を案内
                messagebox.showinfo(
                    "ARKイベントの反映について",
                    getattr(self, "_event_note", "")
                    or "設定は次回のGSM起動で反映されます。", parent=self)
        self.worker.submit(lambda: self.client.ark_event_set(value), done)

    def _fill(self, meta):
        rows = meta["ark"] if isinstance(meta, dict) else meta
        latest = meta.get("latest_build") if isinstance(meta, dict) else None
        self._rows = rows
        self._latest_build = latest
        outdated = [a for a in rows
                    if latest and a.get("build") and str(a["build"]) != str(latest)]
        if latest and outdated:
            self.upd_banner.configure(
                text=f"🆕 更新あり: {len(outdated)}マップ。「⬆ 更新」で最新にできます"
                     "(バージョン表記はクライアントと同じです)", text_color="#ffc27a")
        elif latest:
            self.upd_banner.configure(text="✅ 全マップ最新です", text_color=MUTED)
        else:
            self.upd_banner.configure(text="")
        fill(self.t, [
            (str(a["index"]),
             a["display_name"] + (f"   :{a['game_port']}" if a.get("game_port") else ""),
             (_ark_status_text(a),
              a.get("player_count") if a.get("ready") else "-",
              a.get("public") or "―",
              a.get("uptime_text", "―"),
              ((a.get("version") or "―")
               + (" 🆕" if latest and a.get("build")
                  and str(a["build"]) != str(latest) else ""))),
             (_ark_status_tag(a),)) for a in rows])

    def _sel(self):
        return picked(self, self.t, self._rows, "index", "マップ")

    def _log_fn(self, offset: int = 0):
        s = self.t.selection()
        if not s:
            return None
        idx = int(s[0])
        if idx != self._log_target:      # 別マップを選んだ → 前のログを捨てて取り直す
            self._log_target = idx
            self.log.clear()
            offset = 0
        return lambda: self.client.get(
            f"/api/ark/{idx}/log?lines=250&since={offset}")

    def _sel_indices(self):
        """選択中マップの (indexリスト, 表示名リスト)。未選択なら ([], [])。"""
        sel = self.t.selection()
        rows = [r for r in self._rows if str(r["index"]) in sel]
        return [r["index"] for r in rows], [r["display_name"] for r in rows]

    def _batch(self, action, verb, warn=""):
        idx, names = self._sel_indices()
        if not idx:
            messagebox.showinfo("選択なし", "対象マップを選んでください(複数可)", parent=self)
            return
        head = f"{len(idx)}マップを{verb}しますか?\n" + "・" + "  ・".join(names[:6])
        if len(names) > 6:
            head += f" ほか{len(names)-6}"
        if warn:
            head += "\n\n" + warn
        if not ask(self, head):
            return
        self.act(lambda: self.client.ark_batch(action, idx), f"{verb}({len(idx)}マップ)")

    def _start(self):
        self._batch("start", "起動",
                    "1マップずつ順番に起動します。メモリに注意(1マップ約10GB)。")

    def _stop(self):
        self._batch("stop", "停止", "プレイヤーが居れば60/30/10秒前に予告します。")

    def _restart(self):
        self._batch("restart", "再起動(ローリング)",
                    "1マップずつ順番に再起動します(同時に落ちるのは1つ)。")

    def _update(self):
        from .dialogs import ArkUpdateDialog
        latest = getattr(self, "_latest_build", None)
        maps = [{
            "index": a["index"], "display_name": a["display_name"],
            "version": a.get("version"), "build": a.get("build"),
            "running": a.get("running"),
            "outdated": bool(latest and a.get("build")
                             and str(a["build"]) != str(latest)),
        } for a in self._rows]
        ArkUpdateDialog(
            self.winfo_toplevel(), maps, self.worker,
            run_fn=lambda indices, rolling: self.client.ark_batch(
                "update", indices, rolling=rolling))

    def _pbk(self):
        self.act(self.client.ark_players_backup, "プレイヤーデータBK")

    def _reset_world(self, a):
        from .dialogs import WorldResetDialog
        idx = a["index"]
        WorldResetDialog(
            self.winfo_toplevel(), self.worker,
            a["display_name"], a["display_name"],
            reset_fn=lambda new_seed, backup: self.client.ark_reset_world(
                idx, backup=backup),
            show_seed=False)                          # ARKはシード指定なし

    def _player_cmd(self, idx):
        a = self._sel_silent()
        name = a["display_name"] if a else "ARK"
        from .dialogs import PlayerCommandDialog
        PlayerCommandDialog(
            self.winfo_toplevel(), self.worker, name,
            list_fn=lambda: self.client.ark_rcon(idx, "ListPlayers"),
            run_fn=lambda cmd: self.client.ark_rcon(idx, cmd))

    def _prestore(self):
        from .dialogs import PlayerRestoreDialog
        PlayerRestoreDialog(
            self.winfo_toplevel(), self.worker,
            list_backups_fn=self.client.ark_player_backups,
            list_players_fn=self.client.ark_player_backup_players,
            restore_fn=self.client.ark_players_restore)

    # ---- 右クリックメニュー(すっきり保つため副次操作はここ) ----
    def _menu_items(self):
        a = self._sel_silent()
        if not a:
            return []
        idx = a["index"]
        from core.arkconfig import ARK_MAP_SETTINGS
        items = [
            ("✏ 別名を変更", self._rename),
            ("🎮 プレイヤーにコマンド(飛行/無敵ほか)", lambda: self._player_cmd(idx)),
            ("🚫 BAN管理(BAN/キック/許可リスト)",
             lambda: self._ban_manage(idx, a["display_name"])),
            ("💬 RCONコンソール", self._rcon_console),
            ("⚙ 詳細設定(全マップ共通)", self._settings),
        ]
        if a.get("map_label") in ARK_MAP_SETTINGS:      # 固有設定があるマップだけ
            items.append(("🌋 マップ固有設定(このマップのみ)",
                          lambda: self._map_settings(idx, a["display_name"])))
        items += [
            ("⚡ 動的設定(無停止・色/倍率)", self._dynconfig),
            ("📝 生設定ファイル編集(上級者)", self._raw_settings),
            ("💾 バックアップ/復元", self._backup_dialog),
            ("🔄 ワールドリセット(危険)", lambda: self._reset_world(a)),
            None,
            ("🦕 野生恐竜を今すぐリスポーン(告知あり)",
             lambda: self._quick(idx, "respawn", "恐竜リスポーン")),
            ("💾 保存 (saveworld)", lambda: self._quick(idx, "save", "保存")),
            ("☀ 昼にする", lambda: self._quick(idx, "day", "昼")),
            ("🌙 夜にする", lambda: self._quick(idx, "night", "夜")),
        ]
        return items

    def _sel_silent(self):
        s = self.t.selection()
        if not s:
            return None
        return next((r for r in self._rows if str(r["index"]) == s[0]), None)

    def _rcon_console(self):
        a = self._sel_silent()
        if not a:
            return
        from .dialogs import RconConsole
        RconConsole(self.winfo_toplevel(), a["display_name"], self.worker,
                    lambda cmd: self.client.ark_rcon(a["index"], cmd),
                    hints=[("保存", "saveworld"), ("人数", "ListPlayers")])

    def _ban_manage(self, idx, display):
        from .dialogs import BanManageDialog
        BanManageDialog(
            self.winfo_toplevel(), display, "ark",
            lambda action, target, reason: self.client.ark_moderate(idx, action, target),
            self.worker)

    def _map_settings(self, idx, display):
        from .dialogs import ArkMapSettingsDialog
        ArkMapSettingsDialog(self.winfo_toplevel(), display, idx,
                             self.client, self.worker)

    def _rename(self):
        a = self._sel_silent()
        if not a:
            return
        idx, cur = a["index"], a["display_name"]
        dlg = ctk.CTkInputDialog(
            text=f"新しい別名を入力してください。\n現在: {cur}",
            title="ARK 別名を変更")
        new = (dlg.get_input() or "").strip()
        if not new or new == cur:
            return

        def done(res, err):
            if err:
                messagebox.showerror("別名を変更", str(err), parent=self)
            else:
                self.app.toast(f"別名を「{new}」に変更しました")
        self.worker.submit(lambda: self.client.ark_rename(idx, new), done)

    def _quick(self, idx, action, label):
        # タスクとして実行(📋タスク画面に残る)
        self.act(lambda: self.client.ark_quick(idx, action), label)

    def _settings(self):
        from .dialogs import SettingsEditor, ark_settings_tabs
        a = self._sel_silent() or {}
        ver = a.get("version")
        build = a.get("build") or getattr(self, "_latest_build", None)
        detail = (f"バージョン {ver}" if ver else "") + \
                 (f" / ビルド {build}" if build else "")
        SettingsEditor(
            self.winfo_toplevel(), "⚙ ARK 詳細設定(全マップ共通)",
            ark_settings_tabs(), self.worker,
            fetch_fn=self.client.ark_settings_get,
            save_fn=lambda changes, restart: self.client.ark_settings_set(changes),
            note=(f"{detail}\n" if detail else "") +
                 "設定は全マップ共通です。反映には各マップの再起動が必要です"
                 "(稼働中マップは停止時にiniが上書きされるので、停止中に変更するのが確実)。")

    def _dynconfig(self):
        from .dialogs import DynConfigDialog
        DynConfigDialog(
            self.winfo_toplevel(), self.worker,
            get_fn=self.client.dynconfig,
            save_fn=lambda values, enabled, respawn: self.client.set_dynconfig(
                values=values, enabled=enabled, apply=True, respawn=respawn))

    def _raw_settings(self):
        from .dialogs import RawIniEditor
        RawIniEditor(self.winfo_toplevel(), self.worker,
                     get_fn=self.client.ark_rawconfig_get,
                     save_fn=lambda which, text: self.client.ark_rawconfig_set(which, text))

    def _backup_dialog(self):
        a = self._sel_silent()
        if not a:
            return
        from .dialogs import BackupDialog
        idx = a["index"]
        BackupDialog(
            self.winfo_toplevel(), a["display_name"], self.worker,
            list_fn=lambda: self.client.ark_backups(idx),
            backup_fn=lambda: self.client.ark_backup(idx),
            restore_fn=lambda f: self.client.ark_restore(idx, f),
            note="このマップのセーブをzipで世代管理します。復元はマップを停止してから。")


def _players_cell(s: dict) -> str:
    """人数セル。停止中は「-」、人数が取れない時は「?」(0人と区別する)。"""
    if s.get("status") != "active":
        return "-"
    n = s.get("player_count")
    return "?" if n is None else str(n)


class ServerPage(Page):
    COLS = ("status", "players", "version", "public", "vm", "address")
    _log_target = None          # 今ログを表示している対象(切替検知用)
    H = {"status": ("状態", 100), "players": ("人数", 55), "version": ("バージョン", 110),
         "public": ("外部公開", 85), "vm": ("VM", 110), "address": ("アドレス", 190)}

    def __init__(self, master, app, game, label):
        self.game = game
        self.label = label
        super().__init__(master, app)

    def build(self):
        self._rows = []
        self.title(self.label)
        self.t = tree(self, self.COLS, self.H, "サーバー", 280, 7)
        b = self.bar()
        for txt, act, kind in (("▶ 起動", "start", "primary"), ("■ 停止", "stop", "danger"),
                               ("🔁 再起動", "restart", "normal")):
            self.btn(b, txt, lambda a=act: self._act(a), kind).pack(side="left",
                                                                    padx=(0, 6))
        if self.game == "minecraft":     # 新規構築(バージョン選択)はMCのみ
            self.btn(b, "⚙ 新規構築", self._new_server, "normal").pack(
                side="left", padx=(0, 6))
        ctk.CTkLabel(self, text="ライブログ", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w",
                                                                    pady=(14, 4))
        self.attach_menu(self.t, self._menu_items)
        # ログ専用ワーカー: 停止中サーバーのログ取得はSSHタイムアウトで数秒かかる。
        # 画面更新用ワーカーと共有すると、その間 一覧の更新まで止まって もっさりする。
        self.log = LogView(self, self._log_fn, Worker(self, 20, "srv-log"))
        self.log.pack(fill="both", expand=True)   # 追尾は on_show で開始する
        self.poll(self.client.servers, self._fill)

    def _new_server(self):
        from .dialogs import ProvisionDialog
        ProvisionDialog(
            self.winfo_toplevel(), self.worker,
            templates_fn=self.client.provision_templates,
            provision_fn=self.client.provision,
            vms_fn=self.client.vms,
            versions_fn=self.client.provision_versions,
            task_fn=self.client.task)

    def _fill(self, servers):
        self._rows = [s for s in servers if s["game"] == self.game]
        marks = {"active": "🟢 稼働中", "inactive": "⚪ 停止中", "error": "⚠ 接続不可"}

        def status_cell(s):
            return marks.get(s.get("status"), "…")

        def version_cell(s):
            upd = s.get("update") or {}
            ver = s.get("version") or "―"
            if upd.get("update_available"):
                return f"{ver} 🆕"        # 更新あり
            if s.get("status") == "active" and upd.get("latest"):
                return f"{ver} ✓"         # 確認済み・最新
            return ver
        fill(self.t, [
            (s["name"], s["display_name"],
             (status_cell(s), _players_cell(s), version_cell(s),
              s.get("public") or "―",
              s.get("vm") or "-",
              s.get("fqdn") or s.get("address") or ""),
             ("active" if s.get("status") == "active"
              else "err" if s.get("status") == "error" else "off",))
            for s in self._rows])

    def _sel(self):
        return picked(self, self.t, self._rows, "name", "サーバー")

    def _log_fn(self, offset: int = 0):
        s = self.t.selection()
        if not s:
            return None
        name = s[0]
        if name != self._log_target:     # 別サーバーを選んだ → 取り直す
            self._log_target = name
            self.log.clear()
        # MC/Palworld は journalctl 経由で差分オフセットが使えないので全文取得
        return lambda: self.client.get(f"/api/servers/{name}/log?lines=250")["log"]

    def _act(self, act):
        s = self._sel()
        if not s:
            return
        jp = {"start": "起動", "stop": "停止", "restart": "再起動"}[act]
        if act in ("stop", "restart") and not ask(self, f"{s['display_name']} を{jp}しますか?"):
            return
        self.act(lambda: self.client.server_action(s["name"], act),
                 f"{jp} {s['display_name']}")

    def _sel_silent(self):
        s = self.t.selection()
        if not s:
            return None
        return next((r for r in self._rows if r["name"] == s[0]), None)

    # ---- 右クリックメニュー ----
    def _menu_items(self):
        s = self._sel_silent()
        if not s:
            return []
        items = [("💬 RCONコンソール", self._rcon_console)]
        if self.game == "palworld":
            items.append(("⚙ 詳細設定", lambda: self._pal_settings(s)))
            items.append(("🧠 メモリ変更(VM)", lambda: self._mc_memory(s)))
        if self.game == "minecraft":
            items.append(("⚙ 詳細設定 (server.properties)",
                          lambda: self._mc_settings(s)))
            items.append(("🧩 Mod管理", lambda: self._mc_mods(s)))
            items.append(("🔀 バージョン変更(アップグレード)",
                          lambda: self._mc_version(s)))
            items.append(("🧠 メモリ変更", lambda: self._mc_memory(s)))
        items.append(("💾 バックアップ/復元", lambda: self._backup_dialog(s)))
        items.append(("🚫 BAN管理(BAN/キック/許可リスト)", lambda: self._ban_manage(s)))
        if self.game in ("minecraft", "palworld"):
            items.append(("🔄 ワールドリセット(危険)", lambda: self._reset_world(s)))
        items += [None,
                  ("🌍 外部公開", lambda: self._publish(s, False)),
                  ("🚫 公開を停止", lambda: self._publish(s, True))]
        if self.game == "palworld":       # 更新はPalworldのみ(SteamCMD)
            items += [None,
                      ("🔍 更新を確認", lambda: self._update_check(s)),
                      ("⬆ 更新する", lambda: self._update(s))]
        items += [None, ("🗑 サーバー削除", lambda: self._delete_server(s))]
        return items

    def _ban_manage(self, s):
        from .dialogs import BanManageDialog
        name = s["name"]
        BanManageDialog(
            self.winfo_toplevel(), s.get("display_name") or name, self.game,
            lambda action, target, reason: self.client.server_moderate(
                name, action, target, reason),
            self.worker)

    def _delete_server(self, s):
        from .dialogs import DeleteServerDialog
        DeleteServerDialog(self.winfo_toplevel(), s, self.client, self.worker,
                           on_done=lambda: self.app.toast(
                               "サーバー削除を開始しました(タスク画面で進捗を確認)"))

    def _reset_world(self, s):
        from .dialogs import WorldResetDialog
        name = s["name"]
        WorldResetDialog(
            self.winfo_toplevel(), self.worker, name,
            s.get("display_name") or name,
            reset_fn=lambda new_seed, backup: self.client.server_reset_world(
                name, new_seed=new_seed, backup=backup),
            show_seed=(self.game == "minecraft"))    # シードはMCのみ

    def _publish(self, s, stop):
        name, disp = s["name"], s["display_name"]
        if not stop:
            port = s.get("game") == "palworld" and ":ポート必須" or ":SRVで自動"
            if not ask(self, f"{disp} を外部公開しますか?\n"
                             f"接続名: {s.get('fqdn') or '(FQDN未設定)'}\n"
                             "ルーターにポート転送し、DNSを現WANに向けます。"
                             "ホワイトリスト運用を推奨します。"):
                return
        elif not ask(self, f"{disp} の外部公開を停止しますか?"):
            return
        self.act(lambda: self.client.server_publish(name, unpublish=stop),
                 f"{'公開停止' if stop else '外部公開'} {disp}")

    def _mc_settings(self, s):
        from .dialogs import SettingsEditor, mc_settings_tabs
        name = s["name"]
        self.app.toast("設定を取得中…(VM停止中は起動→取得→停止するため数十秒かかります)")

        def opened(props, err):
            if err:
                messagebox.showerror("設定", str(err), parent=self.winfo_toplevel())
                return
            values = {p["key"]: p["value"] for p in (props or [])}
            tabs = mc_settings_tabs(list(values))
            SettingsEditor(
                self.winfo_toplevel(),
                f"⚙ {s['display_name']} 設定 (server.properties)",
                tabs, self.worker,
                fetch_fn=lambda ids: {"values": values},
                save_fn=lambda ch, rs: self.client.mc_config_set(name, ch, rs),
                note="server.properties を日本語表示しています。変更した項目だけ保存します。",
                restart_label="保存後に再起動して反映する")
        self.worker.submit(lambda: self.client.mc_config_get(name), opened)

    def _mc_mods(self, s):
        from .mod_dialog import ModManager
        ModManager(self.winfo_toplevel(), s, self.client, self.worker)

    def _mc_version(self, s):
        from .dialogs import McVersionDialog
        McVersionDialog(self.winfo_toplevel(), s, self.client, self.worker,
                        on_started=lambda: self.app.toast(
                            "バージョン変更を開始しました(タスク画面で進捗を確認)"))

    def _mc_memory(self, s):
        from .dialogs import MemoryDialog
        MemoryDialog(self.winfo_toplevel(), s, self.client, self.worker,
                     on_done=lambda: self.app.toast(
                         "メモリ変更を開始しました(タスク画面で進捗を確認)"))

    def _backup_dialog(self, s):
        from .dialogs import BackupDialog
        name = s["name"]
        BackupDialog(
            self.winfo_toplevel(), s["display_name"], self.worker,
            list_fn=lambda: self.client.server_backups(name),
            backup_fn=lambda: self.client.server_backup(name),
            restore_fn=lambda f: self.client.server_restore(name, f),
            note="ワールド等をtar.gzで世代管理します。復元前にサーバーを停止推奨。")

    def _pal_settings(self, s):
        from .dialogs import SettingsEditor, pal_settings_tabs
        name = s["name"]
        SettingsEditor(
            self.winfo_toplevel(), f"⚙ {s['display_name']} 詳細設定",
            pal_settings_tabs(), self.worker,
            fetch_fn=lambda keys: self.client.pal_config_get(name, keys),
            save_fn=lambda changes, restart: self.client.pal_config_set(
                name, changes, restart),
            note="変更した項目だけ保存します。反映にはサーバー再起動が必要です。",
            restart_label="保存後に再起動して反映する")

    def _rcon_console(self):
        s = self._sel_silent()
        if not s:
            return
        from .dialogs import RconConsole
        hints = ([("情報", "Info"), ("人数", "ShowPlayers")] if self.game == "palworld"
                 else [("人数", "list")])
        RconConsole(self.winfo_toplevel(), s["display_name"], self.worker,
                    lambda cmd: self.client.server_rcon(s["name"], cmd), hints=hints)

    def _update_check(self, s):
        self.app.toast(f"{s['display_name']} の更新を確認中…")

        def done(res, err):
            if err:
                messagebox.showerror("更新確認", str(err), parent=self)
            elif res.get("update_available"):
                messagebox.showinfo("更新あり",
                    f"{s['display_name']} に更新があります。\n"
                    f"build {res.get('installed')} → {res.get('latest')}\n"
                    "右クリック→「⬆ 更新する」で更新できます。", parent=self)
            else:
                messagebox.showinfo("最新",
                    f"{s['display_name']} は最新です (build {res.get('installed')})。",
                    parent=self)
        self.worker.submit(lambda: self.client.server_update_check(s["name"]), done)

    def _update(self, s):
        if ask(self, f"{s['display_name']} を更新しますか?\n"
                     "停止 → SteamCMDで更新 → 起動 を行います(数分)。"):
            self.act(lambda: self.client.server_update(s["name"]),
                     f"更新 {s['display_name']}")


class VmPage(Page):
    COLS = ("state", "mem", "servers")
    H = {"state": ("状態", 130), "mem": ("メモリ", 110), "servers": ("上で動くサーバー", 340)}

    def build(self):
        self._vms = []
        self.title("🖥 仮想マシン")
        self.t = tree(self, self.COLS, self.H, "VM", 210, 10)
        b = self.bar()
        self.btn(b, "▶ 起動", self._start, "primary").pack(side="left", padx=(0, 6))
        self.btn(b, "■ 停止(安全)", lambda: self._stop(False), "danger").pack(side="left",
                                                                          padx=(0, 6))
        self.btn(b, "⏹ 強制停止", lambda: self._stop(True), "danger").pack(side="left")
        self.btn(b, "📋 クローン", self._clone, "normal").pack(side="left", padx=(6, 0))
        self.btn(b, "🗑 削除", self._delete, "danger").pack(side="left", padx=(6, 0))
        self.btn(b, "🖥 PC再起動", self._host_restart, "danger").pack(side="right")
        ctk.CTkLabel(self, text="VMを止める前に、上のゲームサーバーを保存して停止します"
                     "  /  「PC再起動」はホストを再起動(ARK/VMは再起動後に自動復帰)",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(8, 0))
        self.poll(self.client.vms, self._fill, ms=8000)

    def _host_restart(self):
        from .dialogs import HostRestartDialog
        HostRestartDialog(self.winfo_toplevel(), self.client, self.worker)

    def _fill(self, vms):
        self._vms = vms
        fill(self.t, [
            (v["name"], v["name"],
             ("🟢 Running" if v["state"] == "Running" else f"⚪ {v['state']}",
              f"{v['memory_mb']:,} MB" if v["memory_mb"] else "-",
              ", ".join(v.get("servers") or []) or "-"),
             ("active" if v["state"] == "Running" else "off",)) for v in vms])

    def _clone(self):
        from .dialogs import VmCloneDialog
        VmCloneDialog(self.winfo_toplevel(), self.worker,
                      clone_fn=self.client.vm_clone, vms_fn=self.client.vms)

    def _delete(self):
        v = self._sel()
        if not v:
            return
        on = v.get("servers") or []
        if on:
            messagebox.showinfo(
                "VM削除",
                f"VM {v['name']} には登録サーバー({', '.join(on)})があります。\n"
                "先に Minecraft/Palworld ページの『🗑 サーバー削除』で削除してください。",
                parent=self)
            return
        dlg = ctk.CTkInputDialog(
            text=(f"VM「{v['name']}」と仮想ディスクを完全に削除します(戻せません)。\n"
                  f"削除するにはVM名『{v['name']}』を入力してください:"),
            title="VM削除の確認")
        if (dlg.get_input() or "").strip() != v["name"]:
            return
        self.act(lambda: self.client.vm_delete(v["name"], True), f"VM削除 {v['name']}")

    def _sel(self):
        return picked(self, self.t, self._vms, "name", "VM")

    def _start(self):
        v = self._sel()
        if v:
            self.act(lambda: self.client.vm_start(v["name"]), f"VM起動 {v['name']}")

    def _stop(self, force):
        v = self._sel()
        if not v:
            return
        on = ", ".join(v.get("servers") or []) or "なし"
        if ask(self, f"VM {v['name']} を{'強制' if force else ''}停止しますか?\n\n"
                     f"このVM上のサーバー: {on}\n"
                     "先にゲームサーバーを保存して停止します。"):
            self.act(lambda: self.client.vm_stop(v["name"], force=force),
                     f"VM停止 {v['name']}")


class TaskPage(Page):
    COLS = ("status", "category", "lane", "started", "dur")
    H = {"status": ("結果", 100), "category": ("種別", 120), "lane": ("レーン", 140),
         "started": ("開始", 90), "dur": ("所要", 90)}

    def build(self):
        self.title("📋 タスク")
        self.t = tree(self, self.COLS, self.H, "操作", 290, 8)
        self.t.bind("<<TreeviewSelect>>", self._on_sel)
        self.detail = ctk.CTkTextbox(self, fg_color="#12151a", text_color="#c9d1d9",
                                     font=ctk.CTkFont(family="Consolas", size=11),
                                     corner_radius=8)
        self.detail.pack(fill="both", expand=True, pady=(10, 0))
        b = self.bar()
        self.btn(b, "🧹 履歴をクリア",
                 lambda: self.worker.submit(self.client.tasks_clear)).pack(side="left")
        self.poll(lambda: self.client.tasks(limit=60), self._fill, ms=3000)

    def _fill(self, tasks):
        m = {"success": "✅ 成功", "failed": "❌ 失敗", "running": "⏳ 実行中",
             "pending": "… 待機"}
        fill(self.t, [
            (t["id"], t["title"],
             (m.get(t["status"], t["status"]), t["category"], t["lane"],
              t["started"] or "", f"{t['duration']:.1f}秒" if t["duration"] else ""),
             (t["status"],)) for t in tasks])

    def _on_sel(self, _e=None):
        s = self.t.selection()
        if not s:
            return

        def done(t, err):
            if err:
                return
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0",
                               f"■ {t['title']}\n{t['category']} / {t['lane']} / "
                               f"{t['status']}\n"
                               + (f"エラー: {t['error']}\n" if t.get("error") else "")
                               + "\n" + "\n".join(t["log"]))
            self.detail.configure(state="disabled")
        self.worker.submit(lambda: self.client.task(s[0]), done)


class AddMemberDialog(ctk.CTkToplevel):
    """クラスタにMCサーバーを追加する(共有ON/OFFを選ぶ)。"""

    def __init__(self, master, cluster, available, client, worker, on_done=None):
        super().__init__(master)
        self.cluster = cluster
        self.available = available          # [{server, display}]
        self.client = client
        self.worker = worker
        self.on_done = on_done
        self.title(f"サーバー追加 — {cluster}")
        self.geometry("460x260")
        self.configure(fg_color="#0f1115")

        ctk.CTkLabel(self, text=f"🌐 クラスタ「{cluster}」にサーバーを追加",
                     text_color=TEXT, font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(self, text="追加するとClusterConnect+CombatSwitchを配置し、online-mode=falseにして"
                     "Velocityへ登録します(対象サーバーが再起動します)。",
                     text_color=MUTED, wraplength=420, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=16, pady=(0, 8))

        self._map = {a["display"]: a["server"] for a in available}
        self.menu = ctk.CTkOptionMenu(self, values=list(self._map) or ["(なし)"],
                                      width=280, font=ctk.CTkFont(size=12))
        self.menu.pack(anchor="w", padx=16, pady=4)
        self.share = ctk.CTkSwitch(self, text="アイテム共有ON(InvSync+共有DB)",
                                   onvalue=True, offvalue=False,
                                   font=ctk.CTkFont(size=12))
        self.share.select()
        self.share.pack(anchor="w", padx=16, pady=8)
        ctk.CTkLabel(self, text="※OFFにするとそのワールドは独立インベントリ(移動は可能)。",
                     text_color=MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w", padx=16)
        self.status = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=420,
                                   justify="left", font=ctk.CTkFont(size=11))
        self.status.pack(anchor="w", padx=16, pady=(4, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(12, 12), side="bottom")
        ctk.CTkButton(bar, text="追加", width=90, height=34, corner_radius=6,
                      fg_color=ACCENT, hover_color="#4a86e0",
                      command=self._add).pack(side="right")
        ctk.CTkButton(bar, text="キャンセル", width=90, height=34, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      command=self.destroy).pack(side="right", padx=(0, 8))
        self.after(120, self.lift)

    def _add(self):
        disp = self.menu.get()
        server = self._map.get(disp)
        if not server:
            return
        share = bool(self.share.get())
        from .jobwait import watch_job, _last_log

        def upd(state, task):
            if not self.winfo_exists():
                return
            if state in ("submitted", "running"):
                line = _last_log(task)
                self.status.configure(text=f"⏳ {line or '追加中…'}", text_color=MUTED)
            elif state == "success":
                self.status.configure(
                    text=f"✅ {server} を追加しました(mod配布・Velocity更新済み)。",
                    text_color="#7ee787")
                messagebox.showinfo("サーバー追加",
                                    f"{server} をクラスタに追加しました。", parent=self)
                if callable(self.on_done):
                    self.on_done()
                self.destroy()
            else:
                err = (task or {}).get("error") or "不明なエラー"
                self.status.configure(text=f"❌ 失敗: {err}", text_color="#ff8f8f")
                messagebox.showerror("サーバー追加 失敗", str(err), parent=self)
        watch_job(self, self.worker,
                  lambda: self.client.cluster_add_member(self.cluster, server, share),
                  self.client.task, upd)


class ClusterPage(Page):
    """MCクラスタ管理。1つのVelocity配下でクラスタを複数持てる。共有はメンバー単位。"""

    def build(self):
        self.title("🌐 クラスタ (Minecraft)")
        top = self.bar()
        self.btn(top, "＋ 新規クラスタ", self._new_cluster, "primary").pack(side="left")
        self.btn(top, "🔄 更新", self._refresh, "normal").pack(side="left", padx=6)
        ctk.CTkLabel(top, text="  Velocityプロキシ配下でMC鯖を束ね /s で移動。アイテム共有はメンバーごとON/OFF。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left", padx=8)
        self.wrap = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.wrap.pack(fill="both", expand=True, pady=(10, 0))
        self._avail = []
        self.poll(self.client.clusters, self._render, ms=8000)

    def _render(self, data):
        if not self.winfo_exists():
            return
        for w in self.wrap.winfo_children():
            w.destroy()
        self._avail = data.get("available", [])
        if not data.get("velocity_ok"):
            ctk.CTkLabel(self.wrap, text="⚠ Velocityプロキシ(C:\\Velocity)が見つかりません。"
                         "先にプロキシ構築が必要です。", text_color="#ffd166",
                         font=ctk.CTkFont(size=12)).pack(anchor="w", pady=6)
        clusters = data.get("clusters", [])
        if not clusters:
            ctk.CTkLabel(self.wrap, text="クラスタがありません。「＋ 新規クラスタ」で作成してください。",
                         text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=8)
        for c in clusters:
            self._card(c)

    def _card(self, c):
        card = ctk.CTkFrame(self.wrap, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=6)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(head, text=f"🌐 {c['name']}", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.btn(head, "🗑 削除", lambda n=c["name"]: self._del(n), "danger").pack(side="right")
        self.btn(head, "＋ サーバー追加", lambda n=c["name"]: self._add(n),
                 "normal").pack(side="right", padx=6)
        members = c.get("members", [])
        if not members:
            ctk.CTkLabel(card, text="  メンバーなし", text_color=MUTED,
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=16, pady=(0, 10))
        for m in members:
            self._row(card, c["name"], m)

    def _row(self, card, cname, m):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=2)
        ctk.CTkLabel(row, text=f"•  {m['display']}   ({m['address']})",
                     text_color="#d7dee6", font=ctk.CTkFont(size=12)).pack(side="left")
        self.btn(row, "除外", lambda: self._remove(cname, m["server"]),
                 "normal").pack(side="right")
        sw = ctk.CTkSwitch(row, text="アイテム共有", onvalue=True, offvalue=False,
                           font=ctk.CTkFont(size=11))
        sw.configure(command=lambda: self._toggle(cname, m["server"], bool(sw.get())))
        (sw.select if m["share"] else sw.deselect)()
        sw.pack(side="right", padx=12)

    # ---- 操作 ----
    def _new_cluster(self):
        dlg = ctk.CTkInputDialog(text="クラスタ名(英数字・ハイフン・アンダースコア):",
                                 title="新規クラスタ")
        name = (dlg.get_input() or "").strip()
        if not name:
            return

        def done(res, err):
            if err:
                messagebox.showerror("新規クラスタ", str(err), parent=self)
                return
            self._refresh()
        self.worker.submit(lambda: self.client.cluster_create(name), done)

    def _add(self, cname):
        if not self._avail:
            messagebox.showinfo("サーバー追加",
                                "追加できるMCサーバーがありません(全て他クラスタに所属済み)。",
                                parent=self)
            return
        AddMemberDialog(self.winfo_toplevel(), cname, self._avail,
                        self.client, self.worker, on_done=self._refresh)

    def _del(self, name):
        if not ask(self, f"クラスタ「{name}」を削除しますか?\n"
                   "メンバーはプロキシ設定・共有modが外れ online-mode=true に戻ります。"):
            return
        self.act(lambda: self.client.cluster_delete(name), f"クラスタ削除 {name}")

    def _remove(self, cname, server):
        if not ask(self, f"{server} をクラスタ「{cname}」から除外しますか?\n"
                   "(プロキシ設定・共有modを外し online-mode=true に戻します)"):
            return
        self.act(lambda: self.client.cluster_remove_member(cname, server),
                 f"除外 {server}")

    def _toggle(self, cname, server, want):
        self.act(lambda: self.client.cluster_set_share(cname, server, want),
                 f"共有{'ON' if want else 'OFF'} {server}")

    def _refresh(self):
        self.worker.submit(self.client.clusters,
                           lambda d, e: (e is None) and self._render(d))


_BK_SECTIONS = [("ark", "🦖 ARK"), ("minecraft", "🟩 Minecraft"),
                ("palworld", "🐑 Palworld"), ("other", "🗂 その他(削除済みなど)")]


class BackupPage(Page):
    """バックアップ統合管理。ゲーム別に一覧し、復元/削除/今すぐ取得ができる。

    保持設定(世代数・保持日数・保存先)もここでまとめて変更する。
    """

    def build(self):
        self.title("💾 バックアップ管理")
        top = self.bar()
        self.btn(top, "🔄 更新", self._refresh, "normal").pack(side="left")
        ctk.CTkLabel(top, text="  ゲーム別に世代を一覧。復元・削除・今すぐ取得ができます。",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left", padx=8)

        # 保持設定(世代数・保持日数・保存先)
        cfgcard = ctk.CTkFrame(self, fg_color=CARD, corner_radius=10)
        cfgcard.pack(fill="x", pady=(10, 0))
        row = ctk.CTkFrame(cfgcard, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(row, text="⚙ 保持設定", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(row, text="世代数", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.keep_e = ctk.CTkEntry(row, width=60)
        self.keep_e.pack(side="left", padx=(4, 12))
        ctk.CTkLabel(row, text="保持日数", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.days_e = ctk.CTkEntry(row, width=60)
        self.days_e.pack(side="left", padx=(4, 12))
        self.btn(row, "💾 保存", self._save_settings, "primary").pack(side="right")
        ctk.CTkLabel(cfgcard, text="  ※ 0=その条件では消しません。例) 世代数0＋保持日数30 = 件数無制限で30日保持。"
                     "どちらも設定すると厳しい方が効きます(最新1件は常に残します)。",
                     text_color=MUTED, wraplength=900, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(0, 8))
        self.path_lbl = ctk.CTkLabel(cfgcard, text="", text_color=MUTED,
                                     font=ctk.CTkFont(size=11))
        self.path_lbl.pack(anchor="w", padx=12, pady=(0, 8))

        self.wrap = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.wrap.pack(fill="both", expand=True, pady=(10, 0))
        self._load_settings()
        self.poll(self.client.backups_all, self._render, ms=12000)

    # ---- 保持設定 ----
    def _load_settings(self):
        def done(res, err):
            if err or not self.winfo_exists():
                return
            self.keep_e.delete(0, "end"); self.keep_e.insert(0, str(res.get("keep", 10)))
            self.days_e.delete(0, "end"); self.days_e.insert(0, str(res.get("retention_days", 0)))
            self.path_lbl.configure(text=f"保存先: {res.get('path','')}")
        self.worker.submit(self.client.backup_settings, done)

    def _save_settings(self):
        try:
            keep = max(0, int(self.keep_e.get().strip() or "0"))
            days = max(0, int(self.days_e.get().strip() or "0"))
        except ValueError:
            messagebox.showerror("保持設定", "世代数・保持日数は0以上の整数で入力してください。",
                                 parent=self)
            return

        def done(res, err):
            if err:
                messagebox.showerror("保持設定", str(err), parent=self)
            else:
                self.app.toast("✅ 保持設定を保存しました")
        self.worker.submit(
            lambda: self.client.backup_settings_set(keep=keep, retention_days=days), done)

    # ---- 一覧描画 ----
    def _render(self, targets):
        if not self.winfo_exists():
            return
        # 内容が前回と同じなら再描画しない(12秒毎の全再構築によるチラつき防止)
        sig = tuple((t.get("target"), t.get("count"),
                     tuple(b.get("name") for b in t.get("backups", [])))
                    for t in targets)
        if sig == getattr(self, "_last_sig", None):
            return
        self._last_sig = sig
        for w in self.wrap.winfo_children():
            w.destroy()
        by_game: dict[str, list] = {}
        for t in targets:
            by_game.setdefault(t.get("game", "other"), []).append(t)
        if not targets:
            ctk.CTkLabel(self.wrap, text="バックアップはまだありません。",
                         text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=8)
            return
        for game, label in _BK_SECTIONS:
            items = by_game.get(game)
            if not items:
                continue
            ctk.CTkLabel(self.wrap, text=label, text_color=TEXT,
                         font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                        pady=(10, 2))
            for t in items:
                self._target_card(t)

    def _target_card(self, t):
        card = ctk.CTkFrame(self.wrap, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=5)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(head, text=f"{t['display']}", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text=f"   {t['count']}件 / {t['total_mb']} MB",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left")
        # 今すぐ取得(MC/Palworld=サーバー名 / ARKワールド=idx。孤児・プレイヤーデータは不可)
        can_backup = ((t["game"] in ("minecraft", "palworld"))
                      or (t["game"] == "ark" and t["kind"] == "world"
                          and t.get("idx") is not None))
        if can_backup:
            self.btn(head, "＋ 今すぐ取得", lambda tt=t: self._backup_now(tt),
                     "normal").pack(side="right")
        for b in t["backups"]:
            self._backup_row(card, t, b)

    def _backup_row(self, card, t, b):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=1)
        ctk.CTkLabel(row, text=f"📅 {b['mtime']}    {b['size_mb']} MB",
                     text_color="#d7dee6", font=ctk.CTkFont(size=12)).pack(side="left")
        self.btn(row, "🗑", lambda: self._delete(t, b), "danger").pack(side="right")
        self.btn(row, "↩ 復元", lambda: self._restore(t, b),
                 "normal").pack(side="right", padx=6)

    # ---- 操作 ----
    def _backup_now(self, t):
        game = t["game"]
        if game in ("minecraft", "palworld"):
            fn = lambda: self.client.server_backup(t["target"])
        else:
            fn = lambda: self.client.ark_backup(t["idx"])
        self.act(fn, f"バックアップ {t['display']}")
        self.after(2500, self._refresh)

    def _restore(self, t, b):
        warn = ("復元は既存データを上書きします。対象サーバー/マップは"
                "先に停止してください。\n\n"
                f"対象: {t['display']}\nファイル: {b['name']}\n\n復元しますか?")
        if not ask(self, warn):
            return
        self.act(lambda: self.client.backup_restore_any(t["target"], b["path"]),
                 f"復元 {t['display']}")

    def _delete(self, t, b):
        if not ask(self, f"このバックアップを削除しますか?(元に戻せません)\n\n{b['name']}"):
            return

        def done(res, err):
            if err:
                messagebox.showerror("削除", str(err), parent=self)
            else:
                self.app.toast("🗑 バックアップを削除しました")
                self._refresh()
        self.worker.submit(lambda: self.client.backup_delete(b["path"]), done)

    def _refresh(self):
        self.worker.submit(self.client.backups_all,
                           lambda d, e: (e is None) and self._render(d))


class SettingsPage(Page):
    """アプリ設定のエクスポート/インポート(独自の設定ファイル作成)。"""

    def build(self):
        self.title("⚙ 設定")
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(card, text="設定のインポート / エクスポート", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                    padx=14, pady=(12, 2))
        ctk.CTkLabel(card, text="config.yaml(サーバー・ネットワーク・DNS等)と各種状態"
                     "(通知・予約・ポート開放など)を1ファイルにまとめて保存/復元できます。\n"
                     "パスワードを付けると暗号化され、取り込み時に同じパスワードが必要になります。",
                     text_color=MUTED, wraplength=820, justify="left",
                     font=ctk.CTkFont(size=12)).pack(anchor="w", padx=14, pady=(0, 10))
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(anchor="w", padx=14, pady=(0, 14))
        self.btn(row, "⬆ エクスポート(書き出し)", self._export, "primary").pack(side="left")
        self.btn(row, "⬇ インポート(読み込み)", self._import,
                 "normal").pack(side="left", padx=8)
        ctk.CTkLabel(self, text="※ インポートすると現在の設定は自動でバックアップされます"
                     "(settings-pre-import_*.gsmbackup)。取り込み後はサービス再起動で"
                     "全設定が反映されます。",
                     text_color=MUTED, wraplength=820, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(10, 0))

    def _export(self):
        from .dialogs import ConfigExportDialog
        ConfigExportDialog(self.winfo_toplevel(), self.client, self.worker)

    def _import(self):
        from .dialogs import ConfigImportDialog
        ConfigImportDialog(self.winfo_toplevel(), self.client, self.worker,
                           on_done=lambda: self.app.toast("✅ 設定を取り込みました"))


class NetworkPage(Page):
    """DNS登録状況とポート開放(UPnP)状況を表(Treeview)で一覧する画面。"""

    def build(self):
        self.title("🌐 ネットワーク (DNS / ポート)")
        top = self.bar()
        self.btn(top, "🔄 更新", self._refresh, "normal").pack(side="left")
        self.ps_switch = ctk.CTkSwitch(top, text="自動ポート開放", onvalue=True,
                                       offvalue=False, font=ctk.CTkFont(size=12),
                                       command=self._toggle_portsync)
        self.ps_switch.pack(side="left", padx=16)
        self.btn(top, "🔌 ポート同期(手動)", self._reconcile, "normal").pack(side="left")

        # 状態サマリー(WAN/DNS/自動開放)を1行で。右端に置かず左寄せで見切れ防止。
        self.summary = ctk.CTkLabel(self, text="読み込み中…", text_color=MUTED,
                                    anchor="w", font=ctk.CTkFont(size=11))
        self.summary.pack(fill="x", pady=(8, 2))

        self._dns_by_name: dict = {}

        # ── DNS登録状況 ──
        ctk.CTkLabel(self, text="DNS登録状況", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w",
                                                                    pady=(8, 2))
        self.dns_t = tree(
            self, ["state", "fqdn", "result"],
            {"state": ("状態", 110), "fqdn": ("FQDN", 240), "result": ("解決結果", 300)},
            first="サーバー", first_w=200, height=5)
        for c in ("fqdn", "result"):
            self.dns_t.column(c, anchor="w")
        self.attach_menu(self.dns_t, self._dns_menu)

        # ── サーバー別ポート開放 ──
        ctk.CTkLabel(self, text="サーバー別ポート開放", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w",
                                                                    pady=(10, 2))
        self.psrv_t = tree(
            self, ["state", "ext", "intern"],
            {"state": ("開放", 110), "ext": ("外部ポート", 150), "intern": ("内部", 200)},
            first="サーバー", first_w=200, height=4)
        self.psrv_t.column("intern", anchor="w")

        # ── ルーターの現在のUPnP転送 ──
        ctk.CTkLabel(self, text="ルーターの現在のUPnP転送", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w",
                                                                    pady=(10, 2))
        self.upnp_t = tree(
            self, ["ext", "proto", "dest", "owner"],
            {"ext": ("外部", 90), "proto": ("プロト", 80),
             "dest": ("転送先", 200), "owner": ("所有者", 200)},
            first="説明", first_w=220, height=8)
        for c in ("dest", "owner"):
            self.upnp_t.column(c, anchor="w")

        self.poll(self.client.network, self._render, ms=15000)

    # ---- 操作 ----
    def _toggle_portsync(self):
        want = bool(self.ps_switch.get())

        def done(res, err):
            if err:
                messagebox.showerror("自動ポート開放", str(err), parent=self)
            else:
                self.app.toast(f"自動ポート開放を{'ON' if want else 'OFF'}にしました")
        self.worker.submit(
            lambda: self.client.net_settings_set(portsync_enabled=want), done)

    def _reconcile(self):
        self.act(self.client.ports_reconcile, "ポート同期")

    def _dns_menu(self):
        sel = self.dns_t.selection()
        if not sel:
            return []
        row = self._dns_by_name.get(sel[0])
        if not row or not row.get("fqdn"):
            return []
        items = [("🌐 DNS登録/再登録",
                  lambda: self._dns_register(row["name"], row["display"]))]
        return items

    def _dns_register(self, name, display):
        self.act(lambda: self.client.server_dns_register(name), f"DNS登録 {display}")
        self.after(2500, self._refresh)

    def _refresh(self):
        self.worker.submit(self.client.network,
                           lambda d, e: (e is None) and self._render(d))

    # ---- 描画 ----
    def _render(self, data):
        if not self.winfo_exists():
            return
        ports = data.get("ports", {})
        en = ports.get("enabled")
        if en is None:
            self.ps_switch.configure(state="disabled")
        else:
            (self.ps_switch.select if en else self.ps_switch.deselect)()
        wan = ports.get("wan")
        auto = {True: "ON(起動中だけ開放)", False: "OFF(手動のみ)",
                None: "無効"}.get(en, str(en))
        wtxt = wan if ports.get("gateway_ok") else "ルーター(UPnP)応答なし"
        self.summary.configure(
            text=f"内部DNS: {data.get('resolver') or '(未設定)'}   /   "
                 f"WAN: {wtxt}   /   自動ポート開放: {auto}")

        # DNS
        self._dns_by_name = {r["name"]: r for r in data.get("dns", [])}
        dns_rows = []
        for r in data.get("dns", []):
            badge, tag, result = self._dns_cells(r)
            dns_rows.append((r["name"], r["display"],
                             (badge, r.get("fqdn") or "—", result), [tag]))
        fill(self.dns_t, dns_rows)

        # サーバー別ポート
        prows = []
        for s in ports.get("servers", []):
            ep = s.get("external_port")
            if not ep:
                continue
            opened = s.get("forwarded")
            prows.append((f"psrv:{s['name']}", s["display"],
                          ("🟢 開放中" if opened else "🔒 閉",
                           f"{ep}/{s['proto']}", f":{s.get('game_port')}"),
                          ["active" if opened else "off"]))
        fill(self.psrv_t, prows)

        # UPnP転送一覧
        urows = []
        maps = ports.get("mappings", [])
        for i, m in enumerate(maps):
            urows.append((
                f"map:{i}", m.get("description") or "(名前なし)",
                (m.get("external_port"), m.get("protocol"),
                 f"{m.get('internal_client')}:{m.get('internal_port')}",
                 m.get("owner") or "—"),
                ["active" if m.get("gsm") else "off"]))
        if not ports.get("gateway_ok"):
            urows = [("map:err", "⚠ ルーター(UPnP)に接続できませんでした",
                      ("", "", "", ""), ["err"])]
        fill(self.upnp_t, urows)

    @staticmethod
    def _dns_cells(r):
        """(バッジ, 色タグ, 解決結果テキスト) を返す。"""
        if not r.get("fqdn"):
            return ("— 未設定", "off", "fqdn未設定")
        if r.get("error"):
            return ("🔴 照会失敗", "err", str(r["error"])[:60])
        if not r.get("resolves"):
            return ("🔴 未登録", "err", "DNSに登録なし(右クリックで登録)")
        a = ", ".join(r.get("a", []))
        srv = r.get("srv")
        srvtxt = f"  / SRV→:{srv['port']}" if srv else ""
        if r.get("lan_match"):
            return ("🟢 登録OK", "active", f"{a}(LAN一致){srvtxt}")
        return ("🌐 外部/WAN", "running", f"{a}(外部公開でWAN){srvtxt}")


_PL_SECTIONS = [("ark", "🦖 ARK"), ("minecraft", "🟩 Minecraft"),
                ("palworld", "🐑 Palworld")]


class PlayersPage(Page):
    """各サーバーに今誰が入っているか(接続中プレイヤー名)を一覧する。"""

    def build(self):
        self.title("👥 プレイヤー (接続中)")
        top = self.bar()
        self.btn(top, "🔄 更新", self._refresh, "normal").pack(side="left")
        self.total_lbl = ctk.CTkLabel(top, text="", text_color=TEXT,
                                      font=ctk.CTkFont(size=13, weight="bold"))
        self.total_lbl.pack(side="left", padx=16)
        self.wrap = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.wrap.pack(fill="both", expand=True, pady=(10, 0))
        self.poll(self.client.players_all, self._render, ms=8000)

    def _refresh(self):
        self.worker.submit(self.client.players_all,
                           lambda d, e: (e is None) and self._render(d))

    def _render(self, data):
        if not self.winfo_exists():
            return
        groups = data.get("groups", [])
        total = data.get("total", 0)
        # 内容が同じなら再描画しない(8秒毎の全再構築によるチラつき防止)
        sig = tuple((g.get("id"), g.get("running"), g.get("ready"),
                     tuple(g.get("players", []))) for g in groups)
        if sig == getattr(self, "_last_sig", None):
            return
        self._last_sig = sig
        for w in self.wrap.winfo_children():
            w.destroy()
        self.total_lbl.configure(text=f"合計 {total} 人 接続中")
        running = [g for g in groups if g.get("running")]
        if not running:
            ctk.CTkLabel(self.wrap, text="起動中のサーバーがありません。",
                         text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w",
                                                                           pady=8)
            return
        by_kind: dict[str, list] = {}
        for g in running:
            by_kind.setdefault(g.get("kind", "other"), []).append(g)
        for kind, label in _PL_SECTIONS:
            items = by_kind.get(kind)
            if not items:
                continue
            ctk.CTkLabel(self.wrap, text=label, text_color=TEXT,
                         font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w",
                                                                        pady=(10, 2))
            for g in items:
                self._server_card(g)

    def _server_card(self, g):
        card = ctk.CTkFrame(self.wrap, fg_color=CARD, corner_radius=10)
        card.pack(fill="x", pady=4)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(head, text=g["display"], text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        cnt = g.get("count")
        ready = g.get("ready")
        badge = (f"{cnt} 人" if isinstance(cnt, int) else "―")
        ctk.CTkLabel(head, text=f"   👥 {badge}", text_color=ACCENT,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        if not ready:
            self._note(card, "起動中…(まだ参加できません)")
            return
        if not g.get("known"):
            self._note(card, "取得不可(RCON応答なし)")
            return
        entries = g.get("entries") or [{"name": n, "id": n}
                                       for n in (g.get("players") or [])]
        if not entries:
            self._note(card, "誰もいません")
            return
        for e in entries:
            lb = ctk.CTkLabel(card, text=f"    👤 {e['name']}", text_color="#d7dee6",
                              anchor="w", font=ctk.CTkFont(size=13))
            lb.pack(fill="x", padx=14, pady=1)
            lb.bind("<Button-3>", lambda ev, gg=g, ee=e: self._player_menu(ev, gg, ee))
        ctk.CTkLabel(card, text="    ↳ プレイヤーを右クリックで キック/BAN",
                     text_color=MUTED, font=ctk.CTkFont(size=10)).pack(anchor="w",
                                                                       padx=14, pady=(1, 6))

    def _player_menu(self, ev, g, e):
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg="#e6edf3",
                       activebackground="#2f5c9e", activeforeground="#ffffff",
                       bd=0, font=(ui_font(self), 10))
        menu.add_command(label=f"🚫 {e['name']} をキック",
                         command=lambda: self._quick_moderate(g, e, "kick"))
        menu.add_command(label=f"⛔ {e['name']} をBAN",
                         command=lambda: self._quick_moderate(g, e, "ban"))
        menu.tk_popup(ev.x_root, ev.y_root)

    def _quick_moderate(self, g, e, action):
        verb = "キック" if action == "kick" else "BAN"
        if not ask(self, f"{e['name']} を {verb} しますか?\n"
                   f"(サーバー: {g['display']} / ID: {e['id']})"):
            return

        def call():
            if g["kind"] == "ark":
                idx = int(str(g["id"]).split(":")[1])
                return self.client.ark_moderate(idx, action, e["id"])
            return self.client.server_moderate(g["id"], action, e["id"])

        def done(res, err):
            if err:
                messagebox.showerror(verb, str(err), parent=self)
            else:
                self.app.toast(f"✅ {e['name']} を{verb}しました")
                self._refresh()
        self.worker.submit(call, done)

    @staticmethod
    def _note(card, text):
        ctk.CTkLabel(card, text=f"    {text}", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(anchor="w", padx=14, pady=(0, 8))


class App(ctk.CTk):
    NAV = [("dash", "  ダッシュボード", None),
           (None, "ゲームサーバー", "head"),
           ("players", "     👥  プレイヤー", None),
           ("ark", "     🦖  ARK", None),
           ("pal", "     🐑  Palworld", None),
           ("mc", "     🟩  Minecraft", None),
           ("cluster", "     🌐  クラスタ", None),
           (None, "システム", "head"),
           ("vm", "     🖥  VM", None),
           ("network", "     🌐  ネットワーク", None),
           ("sched", "     ⏰  予約", None),
           ("backup", "     💾  バックアップ", None),
           ("task", "     📋  タスク", None),
           ("notify", "     🔔  通知", None),
           ("settings", "     ⚙  設定", None)]

    def __init__(self, base=DEFAULT_BASE, token: str = ""):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Game Server Manager")
        self.geometry("1280x820")
        self.minsize(1100, 700)
        self.configure(fg_color=BG)
        # customtkinterの既定フォントは "Roboto" で日本語グリフを持たないため、
        # 日本語が代替フォントに落ちて汚く/薄く見える。既定ごと差し替えて統一する。
        ctk.ThemeManager.theme["CTkFont"]["family"] = ui_font(self)
        self.ui_scale = load_scale()
        ctk.set_widget_scaling(UI_SCALES[self.ui_scale])
        style_tree(self, UI_SCALES[self.ui_scale])
        self.client = Client(base, token=token or None)
        self.worker = Worker(self)
        self._pages: dict[str, Page] = {}
        self._cur = None
        self._navbtn: dict[str, ctk.CTkButton] = {}
        # 直接(Hyper-Vなし)モードか。VM/クラスタタブの表示可否に使う(起動時に1回取得)。
        try:
            self._direct = bool(self.client.health().get("direct", False))
        except Exception:
            self._direct = False

        self._head()
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self._side(body)
        self.host = ctk.CTkFrame(body, fg_color="transparent")
        self.host.pack(side="left", fill="both", expand=True, padx=16, pady=(6, 14))
        self.show("dash")
        self._health()
        self._check_update()

    def _check_update(self):
        """起動時にGitHubの新バージョンを1回だけ確認(バックグラウンド)。"""
        def job():
            from core import updatecheck
            return updatecheck.check_latest(GITHUB_REPO, APP_VERSION)

        def done(res, err):
            if err or not res or not res.get("update_available"):
                return
            self._update_url = res.get("url") or self._update_url
            self.update_lbl.configure(
                text=f"🔔 新バージョン {res.get('latest')}(クリック)")
        self.worker.submit(job, done)

    def _open_update(self):
        from core import selfupdate
        # 開発(source)実行では自己更新できないのでリリースページを開く
        if not selfupdate.is_supported():
            import webbrowser
            webbrowser.open(self._update_url)
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
                "アップデート",
                "最新版をダウンロードして更新します。\n"
                "GSM(GUIとサービス)は一度終了し、更新後に自動で再起動します。\n"
                "設定・サーバー・予約などはそのまま引き継がれます。続行しますか?"):
            return

        def job():
            import os
            import tempfile

            def prog(got, total):
                pct = int(got * 100 / total)
                self.after(0, lambda: self.update_lbl.configure(text=f"⬇ 更新DL {pct}%"))

            # インストーラ(Setup.exe)があればそちらを優先(昇格/入替/再起動を丸ごと任せる)
            tag, url = selfupdate.latest_installer(GITHUB_REPO)
            if url:
                dest = os.path.join(tempfile.gettempdir(), "GameServerManager-Setup.exe")
                selfupdate.download(url, dest, progress=prog)
                return ("installer", dest)
            # 無ければ exe 直接入替にフォールバック
            tag, url = selfupdate.latest_exe(GITHUB_REPO)
            if not url:
                raise RuntimeError("最新リリースに更新用ファイルが見つかりません。")
            dest = os.path.join(tempfile.gettempdir(), "GameServerManager.new.exe")
            selfupdate.download(url, dest, progress=prog)
            return ("exe", dest)

        def done(res, err):
            if err:
                self.update_lbl.configure(text="⚠ 更新失敗(クリックで再試行)")
                from tkinter import messagebox
                messagebox.showerror("アップデート失敗", str(err))
                return
            kind, dest = res
            self.update_lbl.configure(text="更新を適用中… 再起動します")
            if kind == "installer":
                selfupdate.run_installer(dest)
            else:
                selfupdate.apply_and_restart(dest)
            import os
            self.after(800, lambda: os._exit(0))

        self.update_lbl.configure(text="⬇ 更新DL 0%")
        self.worker.submit(job, done)

    def _head(self):
        h = ctk.CTkFrame(self, fg_color=SIDE, corner_radius=0, height=48)
        h.pack(fill="x")
        h.pack_propagate(False)
        ctk.CTkLabel(h, text="  ●", text_color=ACCENT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkLabel(h, text=" Game Server Manager", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkLabel(h, text=f" v{APP_VERSION}", text_color=MUTED,
                     font=ctk.CTkFont(size=10)).pack(side="left")
        self.svc = ctk.CTkLabel(h, text="接続確認中…", text_color=MUTED,
                                font=ctk.CTkFont(size=11))
        self.svc.pack(side="right", padx=(4, 14))
        # 裏方サービスを再起動(コード更新の反映など)。ゲーム本体には影響しない。
        ctk.CTkButton(h, text="🔄 サービス再起動", width=118, height=28, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11),
                      command=self._restart_service).pack(side="right", padx=(0, 4))
        # 新バージョン通知(見つかった時だけ表示・クリックでリリース页へ)
        self._update_url = f"https://github.com/{GITHUB_REPO}/releases"
        self.update_lbl = ctk.CTkLabel(h, text="", text_color="#ffc27a", cursor="hand2",
                                       font=ctk.CTkFont(size=11, weight="bold"))
        self.update_lbl.pack(side="right", padx=6)
        self.update_lbl.bind("<Button-1>", lambda _e: self._open_update())
        # 表示サイズ: 画面によって適正が変わるのでユーザーが変えられるようにする
        self._scale_menu = ctk.CTkOptionMenu(
            h, values=list(UI_SCALES), width=78, height=26, corner_radius=6,
            fg_color="#2b303a", button_color="#39404d", button_hover_color="#4a515e",
            font=ctk.CTkFont(size=11), dropdown_font=ctk.CTkFont(size=11),
            command=self._set_scale)
        self._scale_menu.set(self.ui_scale)
        self._scale_menu.pack(side="right", padx=(0, 6))
        ctk.CTkLabel(h, text="表示", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="right", padx=(0, 4))
        self.toast_lb = ctk.CTkLabel(h, text="", text_color=OK,
                                     font=ctk.CTkFont(size=11))
        self.toast_lb.pack(side="right")
        # IPアドレス競合の警告(競合時だけ表示・赤)
        self.conflict_lbl = ctk.CTkLabel(h, text="", text_color="#ff6b6b",
                                         font=ctk.CTkFont(size=12, weight="bold"))
        self.conflict_lbl.pack(side="left", padx=14)

    def _set_scale(self, label: str) -> None:
        self.ui_scale = label
        save_scale(label)
        style_tree(self, UI_SCALES[label])      # 一覧(Treeview)は即反映
        ctk.set_widget_scaling(UI_SCALES[label])
        self.toast(f"表示サイズ {label}(全体に反映するにはアプリを再起動)")

    def _side(self, parent):
        s = ctk.CTkFrame(parent, fg_color=SIDE, width=196, corner_radius=0)
        s.pack(side="left", fill="y")
        s.pack_propagate(False)
        # 直接モードではVM/クラスタは使わないので隠す
        hidden = {"vm", "cluster"} if getattr(self, "_direct", False) else set()
        for key, label, kind in self.NAV:
            if key in hidden:
                continue
            if kind == "head":
                ctk.CTkLabel(s, text=label, text_color=MUTED, anchor="w",
                             font=ctk.CTkFont(size=10, weight="bold")
                             ).pack(fill="x", padx=16, pady=(14, 4))
                continue
            b = ctk.CTkButton(s, text=label, anchor="w", corner_radius=8, height=36,
                              fg_color="transparent", hover_color="#242832",
                              text_color=MUTED, font=ctk.CTkFont(size=12),
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=8, pady=1)
            self._navbtn[key] = b

    def show(self, key: str) -> None:
        if key not in self._pages:
            self._pages[key] = self._make(key)
        if self._cur is not None:
            self._cur.pack_forget()
            if hasattr(self._cur, "on_hide"):   # 隠れたページの更新を止める
                self._cur.on_hide()
        self._cur = self._pages[key]
        self._cur.pack(fill="both", expand=True)
        if hasattr(self._cur, "on_show"):
            self._cur.on_show()
        for k, b in self._navbtn.items():
            on = (k == key)
            b.configure(fg_color=("#2f5c9e" if on else "transparent"),
                        text_color=("#ffffff" if on else MUTED))

    def _make(self, key: str) -> Page:
        if key == "dash":
            d = Dashboard(self.host, self.client, self.worker, on_open=self.show)
            return d
        if key == "ark":
            return ArkPage(self.host, self)
        if key == "pal":
            return ServerPage(self.host, self, "palworld", "🐑 Palworld")
        if key == "mc":
            return ServerPage(self.host, self, "minecraft", "🟩 Minecraft")
        if key == "cluster":
            return ClusterPage(self.host, self)
        if key == "vm":
            return VmPage(self.host, self)
        if key == "sched":
            from .sched_page import SchedPage
            return SchedPage(self.host, self)
        if key == "notify":
            from .notify_page import NotifyPage
            return NotifyPage(self.host, self)
        if key == "backup":
            return BackupPage(self.host, self)
        if key == "settings":
            return SettingsPage(self.host, self)
        if key == "network":
            return NetworkPage(self.host, self)
        if key == "players":
            return PlayersPage(self.host, self)
        return TaskPage(self.host, self)

    def toast(self, text: str) -> None:
        self.toast_lb.configure(text=text)
        self.after(4000, lambda: self.toast_lb.configure(text=""))

    def _restart_service(self) -> None:
        """裏方サービスを再起動する(git pull後のコード反映など)。"""
        if not messagebox.askyesno(
                "サービス再起動",
                "裏方サービス(監視・予約・API)を再起動します。\n"
                "コード更新の反映などに使います。ゲームサーバー本体には影響しません。\n"
                "数秒で戻ります。続行しますか?", icon="warning", default="no"):
            return
        self.svc.configure(text="🔄 サービス再起動中…", text_color=MUTED)

        def job():
            import main_app
            return main_app.restart_service()

        def done(ok, err):
            if err:
                messagebox.showerror("サービス再起動", str(err))
            elif ok:
                self.toast("サービスを再起動しました")
                self._health()
            else:
                messagebox.showwarning(
                    "サービス再起動",
                    "サービスが立ち上がりませんでした。少し待って再度お試しください。")
        self.worker.submit(job, done)

    def _health(self):
        def done(r, err):
            if err is None:
                busy = r.get("busy_lanes") or []
                self.svc.configure(
                    text=f"🟢 接続中   ARK {r['ark_maps']} / サーバー {r['servers']}"
                         + (f"   実行中 {len(busy)}" if busy else ""),
                    text_color=OK)
                conflicts = r.get("ip_conflicts") or []
                if conflicts:
                    txt = "  ".join(
                        f"⚠ IP競合 {c['ip']}: {' / '.join(c['servers'])}"
                        for c in conflicts)
                    self.conflict_lbl.configure(
                        text=txt + " → どちらか停止してください")
                else:
                    self.conflict_lbl.configure(text="")
            else:
                self.svc.configure(text="🔴 サービス未接続", text_color=ERR)
            self.after(4000, self._health)
        self.worker.submit(self.client.health, done)


def _saved_token(base: str) -> str:
    """接続履歴から base に対応する保存済みパスワードを取り出す(無ければ空)。"""
    for r in load_connections().get("recent", []):
        if r.get("base") == base:
            return r.get("token") or ""
    return ""


def run(base: str = DEFAULT_BASE) -> None:
    """ローカル起動の入口。サービスがパスワード必須なら接続画面(ログイン)を出す。
    パスワード未設定なら従来通りそのまま開く。"""
    token = _saved_token(base)
    try:
        Client(base, token=token or None, timeout=5).health()
    except ApiError as e:
        if e.status == 401:                     # 認証必須 → ログイン画面
            sel = connect_screen(base, token)
            if sel:
                run_with_token(sel[0], sel[1])
            return
    except ServiceUnavailable:
        pass                                    # サービス未起動 → 下で警告付きで開く
    run_with_token(base, token)


# ---------------------------------------------------------------------------
# 接続画面(接続専用GUI用): URL/トークンを入れて既存/別PCのサービスに繋ぐ
# ---------------------------------------------------------------------------
def _conn_store():
    from core.paths import app_dir
    return app_dir() / "connections.json"


def load_connections() -> dict:
    import json
    try:
        d = json.loads(_conn_store().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("recent"), list):
            return d
    except Exception:
        pass
    return {"recent": []}


def save_connection(base: str, token: str, remember_token: bool) -> None:
    """接続履歴を保存(先頭が最新・最大8件)。トークンは任意で保存。"""
    import json
    d = load_connections()
    recent = [r for r in d.get("recent", []) if r.get("base") != base]
    recent.insert(0, {"base": base, "token": token if remember_token else ""})
    d["recent"] = recent[:8]
    try:
        _conn_store().write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception:
        pass


def connect_screen(default_base: str = DEFAULT_BASE, default_token: str = ""):
    """接続先を入力する画面。接続成功で (base, token) を返す。閉じたら None。"""
    result = {"base": None, "token": ""}
    win = ctk.CTk()
    ctk.set_appearance_mode("dark")
    ctk.ThemeManager.theme["CTkFont"]["family"] = ui_font(win)
    win.title("GSM に接続")
    win.geometry("470x440")
    win.minsize(430, 420)
    win.configure(fg_color=BG)

    recent = load_connections().get("recent", [])
    if default_base == DEFAULT_BASE and recent:      # 既定は履歴の先頭を優先
        default_base = recent[0].get("base") or default_base
        if not default_token:
            default_token = recent[0].get("token") or ""

    ctk.CTkLabel(win, text="🎮 GSM に接続",
                 font=(ui_font(win), 20, "bold")).pack(pady=(22, 2))
    ctk.CTkLabel(win, text="接続先の常駐サービス(GSM)のURLを入力してください",
                 text_color=MUTED).pack(pady=(0, 14))

    frm = ctk.CTkFrame(win, fg_color="transparent")
    frm.pack(fill="x", padx=28)

    ctk.CTkLabel(frm, text="接続先URL", anchor="w", text_color=MUTED).pack(fill="x")
    url_var = tk.StringVar(value=default_base)
    url_ent = ctk.CTkEntry(frm, textvariable=url_var, height=38,
                           placeholder_text="http://127.0.0.1:8770")
    url_ent.pack(fill="x", pady=(2, 12))

    ctk.CTkLabel(frm, text="パスワード（未設定なら空）",
                 anchor="w", text_color=MUTED).pack(fill="x")
    tok_var = tk.StringVar(value=default_token)
    tok_ent = ctk.CTkEntry(frm, textvariable=tok_var, height=38, show="●")
    tok_ent.pack(fill="x", pady=(2, 8))

    remember = tk.BooleanVar(value=bool(default_token))
    ctk.CTkCheckBox(frm, text="パスワードを保存する", variable=remember,
                    onvalue=True, offvalue=False).pack(anchor="w", pady=(0, 6))

    if recent:                                        # 履歴クイック選択
        row = ctk.CTkFrame(frm, fg_color="transparent")
        row.pack(fill="x", pady=(4, 2))
        ctk.CTkLabel(row, text="履歴:", text_color=MUTED).pack(side="left", padx=(0, 6))

        def _use(r):
            url_var.set(r.get("base", ""))
            tok_var.set(r.get("token", ""))
        for r in recent[:4]:
            short = r.get("base", "").replace("http://", "").replace("https://", "")
            ctk.CTkButton(row, text=short, width=10, height=26, fg_color=CARD,
                          command=lambda r=r: _use(r)).pack(side="left", padx=2)

    status = ctk.CTkLabel(win, text="", text_color=MUTED, wraplength=400)
    status.pack(pady=(10, 0), padx=28)

    def do_connect(*_):
        base = url_var.get().strip()
        token = tok_var.get().strip()
        if not base:
            status.configure(text="URLを入力してください", text_color=ERR)
            return
        if "://" not in base:
            base = "http://" + base
        btn.configure(state="disabled", text="接続中…")
        status.configure(text="接続を確認しています…", text_color=MUTED)

        def work():
            ok, err = False, ""
            try:
                Client(base, token=token or None, timeout=6).health()
                ok = True
            except Exception as e:                    # noqa: BLE001
                err = str(e)

            def done():
                if ok:
                    save_connection(base, token, remember.get())
                    result["base"], result["token"] = base, token
                    win.destroy()
                else:
                    btn.configure(state="normal", text="接続")
                    status.configure(text="接続できませんでした: " + err,
                                     text_color=ERR)
            win.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    btn = ctk.CTkButton(win, text="接続", height=42,
                        font=(ui_font(win), 15, "bold"), command=do_connect)
    btn.pack(fill="x", padx=28, pady=(14, 10))
    url_ent.bind("<Return>", do_connect)
    tok_ent.bind("<Return>", do_connect)
    url_ent.focus_set()
    win.mainloop()
    return (result["base"], result["token"]) if result["base"] else None


def run_with_token(base: str, token: str = "") -> None:
    app = App(base, token=token)
    if not app.client.alive():
        messagebox.showwarning(
            "GSMサービスに接続できません",
            "接続先の常駐サービスが動いていないようです。\n"
            f"接続先: {app.client.base}", parent=app)
    app.mainloop()


def run_connect(base: str | None = None, token: str = "") -> None:
    """接続専用GUIの入口。URL未指定なら接続画面を出す。指定時は直接繋ぐ。"""
    if base:
        run_with_token(base, token)
        return
    sel = connect_screen(DEFAULT_BASE, token)
    if sel:
        run_with_token(sel[0], sel[1])
