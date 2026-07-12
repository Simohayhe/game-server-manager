"""初回起動時の「動作環境チェック」画面。

このPCでHyper-V版が使えるかを表示し、使えない場合はVMを使わない軽量版へ誘導する。
show() は "proceed"(本体を起動) か "quit"(終了) を返す。
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import font as tkfont

_BG = "#1b2430"
_CARD = "#232f3e"
_FG = "#e6edf3"
_MUTED = "#9aa7b4"
_OK = "#3fb950"
_NG = "#ff6b6b"
_ACCENT = "#2f81f7"
_WARN = "#ffd166"


def show(result: dict, auto_close_ms: int | None = None) -> str:
    root = tk.Tk()
    root.title("ゲームサーバーマネージャー — 動作環境チェック")
    root.configure(bg=_BG)
    root.geometry("580x560")
    root.minsize(520, 500)
    action = {"v": "quit"}
    if auto_close_ms:                       # テスト用: 一定時間で自動クローズ
        root.after(auto_close_ms, root.destroy)

    h1 = tkfont.Font(family="Yu Gothic UI", size=16, weight="bold")
    h2 = tkfont.Font(family="Yu Gothic UI", size=11, weight="bold")
    base = tkfont.Font(family="Yu Gothic UI", size=10)

    tk.Label(root, text="ようこそ 🎮", bg=_BG, fg=_FG, font=h1).pack(pady=(18, 2))
    tk.Label(root, text="このPCでゲームサーバーマネージャー(Hyper-V版)が使えるか確認します",
             bg=_BG, fg=_MUTED, font=base).pack(pady=(0, 12))

    # チェック結果カード
    card = tk.Frame(root, bg=_CARD, padx=16, pady=12)
    card.pack(fill=tk.X, padx=18)
    for name, ok, hint in result["checks"]:
        row = tk.Frame(card, bg=_CARD)
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text="✓" if ok else "✗", bg=_CARD,
                 fg=_OK if ok else _NG, font=h2, width=2).pack(side=tk.LEFT)
        col = tk.Frame(row, bg=_CARD)
        col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(col, text=name, bg=_CARD, fg=_FG, font=base, anchor="w"
                 ).pack(fill=tk.X)
        tk.Label(col, text=hint, bg=_CARD, fg=_MUTED, font=("Yu Gothic UI", 8),
                 anchor="w").pack(fill=tk.X)

    # 判定メッセージ + ボタン
    box = tk.Frame(root, bg=_BG)
    box.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)

    def proceed():
        action["v"] = "proceed"
        root.destroy()

    def quit_():
        action["v"] = "quit"
        root.destroy()

    def open_url():
        try:
            webbrowser.open(result.get("vmless_url", ""))
        except Exception:
            pass

    def btn(parent, text, cmd, bg=_ACCENT, fg="#ffffff"):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         font=h2, relief=tk.FLAT, padx=16, pady=8,
                         activebackground=bg, cursor="hand2")

    if result["suitable"]:
        tk.Label(box, text="✅ このPCで利用できます", bg=_BG, fg=_OK,
                 font=h2).pack(anchor="w")
        tk.Label(box, text="「始める」を押すとアプリが起動します。\n"
                           "初回は config.yaml を自分の環境に合わせて編集してください。",
                 bg=_BG, fg=_MUTED, font=base, justify="left").pack(anchor="w", pady=(4, 12))
        btn(box, "▶ 始める", proceed, bg=_OK).pack(anchor="w")
    else:
        reason = result.get("reason")
        if reason == "permission":
            tk.Label(box, text="△ Hyper-Vはありますが、操作する権限が必要です",
                     bg=_BG, fg=_WARN, font=h2).pack(anchor="w")
            tk.Label(box, justify="left", bg=_BG, fg=_MUTED, font=base,
                     text="実行ユーザーを「Hyper-V Administrators」グループに追加して\n"
                          "一度サインアウト→再ログオン、または管理者として起動してください。"
                     ).pack(anchor="w", pady=(4, 12))
            btn(box, "それでも起動する", proceed, bg=_ACCENT).pack(anchor="w", pady=(0, 6))
        else:
            tk.Label(box, text="✗ このPCではHyper-V版を使えません",
                     bg=_BG, fg=_NG, font=h2).pack(anchor="w")
            msg = ("Hyper-VはWindows Pro / Enterprise / Education が必要です。"
                   if reason in ("no_hyperv", "os") else "Hyper-Vが利用できません。")
            tk.Label(box, justify="left", bg=_BG, fg=_MUTED, font=base,
                     text=msg + "\n\nHyper-Vを使わない『軽量版』(SSHで届くLinux/Dockerを\n"
                          "直接管理)をご利用ください。下のボタンから案内ページを開けます。"
                     ).pack(anchor="w", pady=(4, 12))
            btn(box, "🌐 軽量版のページを開く", open_url, bg=_ACCENT).pack(anchor="w")
            tk.Label(box, text="", bg=_BG).pack()
            btn(box, "それでも起動する(上級者向け)", proceed, bg=_CARD, fg=_MUTED
                ).pack(anchor="w", pady=(6, 0))

    btn(box, "終了", quit_, bg=_CARD, fg=_MUTED).pack(anchor="w", pady=(10, 0))

    root.mainloop()
    return action["v"]
