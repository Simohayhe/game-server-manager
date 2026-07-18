"""モダン(ダーク)テーマ。全画面で共通の見た目をここに集約する。

見切れ対策(重要):
  過去に2回「ボタンの文字が見切れる」と指摘されている(2026-07-10, 2026-07-17)。
  原因は ttk のボタンに固定幅/詰めすぎのpaddingを与え、日本語+絵文字の実幅を
  見誤ること。対策として:
    - ボタンは width を固定せず、padding を十分に取って内容に合わせて伸ばす
    - ツールバーは pack ではなく grid + columnconfigure(weight) で余白を分配
    - 収まらない時は自動で折り返す ToolBar を使う(下記)
  これで解像度やDPIが変わっても文字が切れない。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ダークパレット(落ち着いた青系アクセント)
C = {
    "bg":        "#0f1419",   # 最背面
    "sidebar":   "#141a22",   # 左ナビ
    "panel":     "#1a212b",   # カード/一覧
    "panel_alt": "#212a36",   # 一覧の縞/ヘッダ
    "border":    "#2a3542",
    "text":      "#e6edf3",
    "muted":     "#8b98a5",
    "accent":    "#4a9eff",
    "accent_dk": "#2f7fd6",
    "ok":        "#3fb950",
    "warn":      "#d29922",
    "err":       "#f85149",
    "console":   "#0b0f14",
}
# 状態タグの色(一覧で使う)
TAGS = {"active": C["ok"], "off": C["muted"], "err": C["err"],
        "success": C["ok"], "failed": C["err"], "running": C["accent"],
        "pending": C["muted"]}


def fonts(root: tk.Misc) -> dict:
    """日本語が綺麗に出るフォントを選ぶ(無ければ既定にフォールバック)。"""
    fams = set(tkfont.families(root))
    base = next((f for f in ("Yu Gothic UI", "Meiryo UI", "MS UI Gothic", "Segoe UI")
                 if f in fams), "TkDefaultFont")
    mono = next((f for f in ("Consolas", "MS Gothic", "Courier New")
                 if f in fams), "TkFixedFont")
    return {
        "base": (base, 10),
        "bold": (base, 10, "bold"),
        "title": (base, 15, "bold"),
        "nav": (base, 11),
        "small": (base, 9),
        "mono": (mono, 9),
    }


def apply(root: tk.Misc) -> dict:
    """テーマを適用し、フォント定義を返す。"""
    f = fonts(root)
    st = ttk.Style(root)
    try:
        st.theme_use("clam")          # clamは色指定が効きやすい
    except tk.TclError:
        pass
    root.configure(bg=C["bg"])

    st.configure(".", background=C["bg"], foreground=C["text"], font=f["base"],
                 borderwidth=0, focuscolor=C["accent"])
    st.configure("TFrame", background=C["bg"])
    st.configure("Card.TFrame", background=C["panel"])
    st.configure("Sidebar.TFrame", background=C["sidebar"])
    st.configure("Header.TFrame", background=C["panel_alt"])

    st.configure("TLabel", background=C["bg"], foreground=C["text"], font=f["base"])
    st.configure("Card.TLabel", background=C["panel"], foreground=C["text"])
    st.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"],
                 font=f["small"])
    st.configure("CardMuted.TLabel", background=C["panel"], foreground=C["muted"],
                 font=f["small"])
    st.configure("Title.TLabel", background=C["bg"], foreground=C["text"],
                 font=f["title"])
    st.configure("Header.TLabel", background=C["panel_alt"], foreground=C["text"])
    st.configure("Sidebar.TLabel", background=C["sidebar"], foreground=C["muted"],
                 font=f["small"])

    # ボタン: width固定なし + 余裕あるpadding = 文字が見切れない
    st.configure("TButton", background=C["panel_alt"], foreground=C["text"],
                 padding=(14, 8), font=f["base"], relief="flat", borderwidth=0)
    st.map("TButton",
           background=[("active", C["border"]), ("pressed", C["accent_dk"]),
                       ("disabled", C["panel"])],
           foreground=[("disabled", C["muted"])])
    st.configure("Accent.TButton", background=C["accent"], foreground="#ffffff",
                 padding=(14, 8), font=f["bold"])
    st.map("Accent.TButton", background=[("active", C["accent_dk"]),
                                         ("pressed", C["accent_dk"])])
    st.configure("Danger.TButton", background="#3a2226", foreground=C["err"],
                 padding=(14, 8))
    st.map("Danger.TButton", background=[("active", "#4d2a30")])

    # 一覧
    st.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                 foreground=C["text"], rowheight=30, borderwidth=0, font=f["base"])
    st.configure("Treeview.Heading", background=C["panel_alt"],
                 foreground=C["muted"], relief="flat", padding=(8, 8),
                 font=f["small"], borderwidth=0)
    st.map("Treeview.Heading", background=[("active", C["border"])])
    st.map("Treeview", background=[("selected", C["accent_dk"])],
           foreground=[("selected", "#ffffff")])

    st.configure("TEntry", fieldbackground=C["panel_alt"], foreground=C["text"],
                 insertcolor=C["text"], padding=(8, 6), borderwidth=0)
    st.configure("TCombobox", fieldbackground=C["panel_alt"], background=C["panel_alt"],
                 foreground=C["text"], arrowcolor=C["muted"], padding=(6, 5))
    st.configure("TCheckbutton", background=C["bg"], foreground=C["text"],
                 focuscolor=C["bg"])
    st.map("TCheckbutton", background=[("active", C["bg"])])
    st.configure("Card.TCheckbutton", background=C["panel"], foreground=C["text"])
    st.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    st.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"],
                 padding=(16, 9), font=f["base"], borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", C["panel"])],
           foreground=[("selected", C["text"])])
    st.configure("TSeparator", background=C["border"])
    st.configure("Vertical.TScrollbar", background=C["panel_alt"],
                 troughcolor=C["bg"], arrowcolor=C["muted"], borderwidth=0)
    st.configure("TPanedwindow", background=C["bg"])
    return f


class ToolBar(ttk.Frame):
    """収まらなければ自動で折り返すツールバー。

    ボタンをpackで横一列に並べると、幅が足りない時に文字が見切れる(過去2回の指摘)。
    ここでは実測幅を見て grid で折り返すので、どの解像度・DPIでも切れない。

    実装上の注意(一度踏んだ):
      行フレームを毎回 destroy→再生成すると <Configure> が再発火して無限ループになり、
      破棄済みウィジェットへの pack で TclError も出る。そのため
        - ウィジェットは作り直さず grid で置き直すだけ
        - 幅が変わっていない時は何もしない(再入防止)
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._buttons: list[ttk.Button] = []
        self._last_width = -1
        self.bind("<Configure>", self._on_configure)

    def add(self, text: str, command, style: str = "TButton") -> ttk.Button:
        b = ttk.Button(self, text=text, command=command, style=style)
        self._buttons.append(b)
        self._last_width = -1                 # 次のレイアウトで並べ直す
        self.after_idle(lambda: self._relayout(self.winfo_width()))
        return b

    def _on_configure(self, event) -> None:
        if event.width != self._last_width:   # 幅が変わった時だけ = 再入しない
            self._relayout(event.width)

    def _relayout(self, width: int) -> None:
        if width <= 1 or not self._buttons:
            return
        self._last_width = width
        row = col = used = 0
        for b in self._buttons:
            w = b.winfo_reqwidth() + 8
            if used + w > width and col > 0:   # 入らない → 次の行へ
                row += 1
                col = 0
                used = 0
            b.grid(in_=self, row=row, column=col, padx=3, pady=3, sticky=tk.W)
            col += 1
            used += w
