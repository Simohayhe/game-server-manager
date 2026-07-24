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

from .client import Client
from .dashboard import Dashboard
from .widgets import ACCENT, CARD, ERR, MUTED, OK, TEXT, LogView

DEFAULT_BASE = "http://127.0.0.1:8770"
APP_VERSION = "3.5.0"                            # リリースtagと比較して更新通知を出す
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
    def on_show(self) -> None:
        if self._visible:
            return
        self._visible = True
        for spec in self._polls:
            self._run_poll(spec)
        log = getattr(self, "log", None)
        if log is not None:
            log.start()

    def on_hide(self) -> None:
        self._visible = False          # 次のtickで自然に止まる
        log = getattr(self, "log", None)
        if log is not None:
            log.stop()                 # ライブログの追尾も止める(3秒毎の無駄を消す)

    def poll(self, fn, ok, ms=5000):
        """表示中だけ回すポーリングを登録する。"""
        spec = (fn, ok, ms)
        self._polls.append(spec)
        if self._visible:
            self._run_poll(spec)

    def _run_poll(self, spec) -> None:
        fn, ok, ms = spec

        def done(res, err):
            if not self._alive or not self.winfo_exists() or not self._visible:
                return                 # 非表示になったら止める
            if err is None:
                ok(res)
            self.after(ms, lambda: self._run_poll(spec) if self._visible else None)
        self.worker.submit(fn, done)

    def act(self, fn, label):
        def done(res, err):
            if err:
                messagebox.showerror(label, str(err), parent=self)
            else:
                self.app.toast(f"{label} を受け付けました(📋タスクで進捗)")
        self.worker.submit(fn, done)

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
        ctk.CTkLabel(opts, text="  複数選択(Ctrl/Shift)で一括操作・起動/更新は1マップずつ",
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
        return [
            ("✏ 別名を変更", self._rename),
            ("🎮 プレイヤーにコマンド(飛行/無敵ほか)", lambda: self._player_cmd(idx)),
            ("💬 RCONコンソール", self._rcon_console),
            ("⚙ 詳細設定(全マップ共通)", self._settings),
            ("⚡ 動的設定(無停止・色/倍率)", self._dynconfig),
            ("📝 生設定ファイル編集(上級者)", self._raw_settings),
            ("💾 バックアップ/復元", self._backup_dialog),
            None,
            ("🦕 野生恐竜を今すぐリスポーン(告知あり)",
             lambda: self._quick(idx, "respawn", "恐竜リスポーン")),
            ("💾 保存 (saveworld)", lambda: self._quick(idx, "save", "保存")),
            ("☀ 昼にする", lambda: self._quick(idx, "day", "昼")),
            ("🌙 夜にする", lambda: self._quick(idx, "night", "夜")),
        ]

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
            vms_fn=self.client.vms)

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
        if self.game == "minecraft":
            items.append(("⚙ 詳細設定 (server.properties)",
                          lambda: self._mc_settings(s)))
            items.append(("🧩 Mod管理", lambda: self._mc_mods(s)))
        items.append(("💾 バックアップ/復元", lambda: self._backup_dialog(s)))
        if self.game == "minecraft":
            items.append(("🔄 ワールドリセット(危険)", lambda: self._reset_world(s)))
        items += [None,
                  ("🌍 外部公開", lambda: self._publish(s, False)),
                  ("🚫 公開を停止", lambda: self._publish(s, True))]
        if self.game == "palworld":       # 更新はPalworldのみ(SteamCMD)
            items += [None,
                      ("🔍 更新を確認", lambda: self._update_check(s)),
                      ("⬆ 更新する", lambda: self._update(s))]
        return items

    def _reset_world(self, s):
        from .dialogs import WorldResetDialog
        WorldResetDialog(self.winfo_toplevel(), self.worker, s["name"],
                         s.get("display_name") or s["name"],
                         reset_fn=self.client.server_reset_world)

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
        from .dialogs import PropsEditor
        name = s["name"]
        PropsEditor(self.winfo_toplevel(), s["display_name"], self.worker,
                    fetch_fn=lambda: self.client.mc_config_get(name),
                    save_fn=lambda ch, rs: self.client.mc_config_set(name, ch, rs))

    def _mc_mods(self, s):
        from .mod_dialog import ModManager
        ModManager(self.winfo_toplevel(), s, self.client, self.worker)

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
        ctk.CTkLabel(self, text="VMを止める前に、上のゲームサーバーを保存して停止します",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                       pady=(8, 0))
        self.poll(self.client.vms, self._fill, ms=8000)

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


class App(ctk.CTk):
    NAV = [("dash", "  ダッシュボード", None),
           (None, "ゲームサーバー", "head"),
           ("ark", "     🦖  ARK", None),
           ("pal", "     🐑  Palworld", None),
           ("mc", "     🟩  Minecraft", None),
           (None, "システム", "head"),
           ("vm", "     🖥  VM", None),
           ("sched", "     ⏰  予約", None),
           ("task", "     📋  タスク", None),
           ("notify", "     🔔  通知", None)]

    def __init__(self, base=DEFAULT_BASE):
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
        self.client = Client(base)
        self.worker = Worker(self)
        self._pages: dict[str, Page] = {}
        self._cur = None
        self._navbtn: dict[str, ctk.CTkButton] = {}

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
        for key, label, kind in self.NAV:
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
        if key == "vm":
            return VmPage(self.host, self)
        if key == "sched":
            from .sched_page import SchedPage
            return SchedPage(self.host, self)
        if key == "notify":
            from .notify_page import NotifyPage
            return NotifyPage(self.host, self)
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
            else:
                self.svc.configure(text="🔴 サービス未接続", text_color=ERR)
            self.after(4000, self._health)
        self.worker.submit(self.client.health, done)


def run(base: str = DEFAULT_BASE) -> None:
    app = App(base)
    if not app.client.alive():
        messagebox.showwarning(
            "GSMサービスに接続できません",
            "常駐サービス(main_service.py)が動いていないようです。\n"
            f"接続先: {app.client.base}", parent=app)
    app.mainloop()
