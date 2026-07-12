"""初回セットアップ・ウィザード。

config.yaml が無いとき、必要な項目をフォームで入力させて config.yaml を生成する。
YAMLを手編集させないためのもの。run(config_path) は True(作成した/既にある) を返し、
キャンセルなら False。サーバー自体はアプリ内「新規サーバー構築」で後から追加する。
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox

_BG = "#1b2430"
_CARD = "#232f3e"
_FG = "#e6edf3"
_MUTED = "#9aa7b4"
_ACCENT = "#2f81f7"
_OK = "#3fb950"
_FIELD = "#2b3847"


def _build_yaml(v: dict) -> str:
    """フォーム値から config.yaml のテキストを組み立てる(コメント付き)。"""
    lines = ["# ゲームサーバーマネージャー 設定(セットアップウィザードが生成)", ""]
    # hyperv
    lines.append("hyperv:")
    if v["mode"] == "ssh":
        lines += [
            "  mode: ssh",
            f"  host: {v['ssh_host']}",
            f"  user: {v['ssh_user']}",
            f"  password: {v['ssh_pass']}",
        ]
    else:
        lines.append("  mode: local")
    lines.append("")
    # network
    lines += [
        "network:",
        f"  subnet: {v['subnet']}",
        f"  vm_range: {v['vm_range']}",
        f"  gateway: {v['gateway']}",
        "",
    ]
    # dns(任意)
    if v["dns_enabled"]:
        lines += [
            "dns:",
            f"  host: {v['dns_host']}",
            f"  domain: {v['dns_domain']}",
            "  ssh:",
            f"    user: {v['dns_user']}",
            f"    password: {v['dns_pass']}",
            "",
        ]
    # curseforge(任意)
    if v["cf_key"].strip():
        lines += ["curseforge:", f"  api_key: '{v['cf_key'].strip()}'", ""]
    # backup(既定)
    lines += [
        "backup:",
        "  path: 'C:\\GameBackups'",
        "  keep: 10",
        "  compress: true",
        "",
        "# サーバーはアプリの「⚙ 新規サーバー構築」で追加できます",
        "servers: {}",
        "",
    ]
    return "\n".join(lines)


def run(config_path: str | Path, auto_close_ms: int | None = None) -> bool:
    config_path = Path(config_path)
    if config_path.exists():
        return True

    root = tk.Tk()
    root.title("ゲームサーバーマネージャー — 初回セットアップ")
    root.configure(bg=_BG)
    root.geometry("620x680")
    result = {"ok": False}
    if auto_close_ms:                       # テスト用: 一定時間で自動クローズ
        root.after(auto_close_ms, root.destroy)

    h1 = tkfont.Font(family="Yu Gothic UI", size=15, weight="bold")
    h2 = tkfont.Font(family="Yu Gothic UI", size=11, weight="bold")
    base = tkfont.Font(family="Yu Gothic UI", size=10)

    tk.Label(root, text="初回セットアップ ⚙", bg=_BG, fg=_FG, font=h1).pack(pady=(16, 2))
    tk.Label(root, text="必要な項目を入力すると config.yaml を作成します(後から編集も可能)",
             bg=_BG, fg=_MUTED, font=base).pack(pady=(0, 10))

    body = tk.Frame(root, bg=_BG)
    body.pack(fill=tk.BOTH, expand=True, padx=18)

    entries: dict = {}

    def section(title):
        lf = tk.LabelFrame(body, text=title, bg=_CARD, fg=_FG, font=h2,
                           padx=12, pady=8, labelanchor="nw")
        lf.pack(fill=tk.X, pady=6)
        return lf

    def field(parent, key, label, default="", show=None, hint=""):
        row = tk.Frame(parent, bg=_CARD)
        row.pack(fill=tk.X, pady=3)
        tk.Label(row, text=label, bg=_CARD, fg=_FG, font=base, width=18,
                 anchor="w").pack(side=tk.LEFT)
        var = tk.StringVar(value=default)
        e = tk.Entry(row, textvariable=var, show=show, bg=_FIELD, fg=_FG,
                     insertbackground=_FG, relief=tk.FLAT)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        entries[key] = var
        if hint:
            tk.Label(parent, text=hint, bg=_CARD, fg=_MUTED,
                     font=("Yu Gothic UI", 8), anchor="w").pack(fill=tk.X)
        return row

    # --- 動作モード ---
    sec_mode = section("動作モード")
    mode_var = tk.StringVar(value="local")
    entries["mode"] = mode_var
    tk.Radiobutton(sec_mode, text="このPCがHyper-Vホスト(推奨)", variable=mode_var,
                   value="local", bg=_CARD, fg=_FG, selectcolor=_FIELD,
                   font=base, anchor="w", command=lambda: _toggle()).pack(fill=tk.X)
    tk.Radiobutton(sec_mode, text="別PCからSSHでHyper-Vホストを管理", variable=mode_var,
                   value="ssh", bg=_CARD, fg=_FG, selectcolor=_FIELD,
                   font=base, anchor="w", command=lambda: _toggle()).pack(fill=tk.X)
    ssh_box = tk.Frame(sec_mode, bg=_CARD)
    ssh_box.pack(fill=tk.X)
    field(ssh_box, "ssh_host", "ホストIP", "192.168.11.10")
    field(ssh_box, "ssh_user", "Windowsユーザー", "")
    field(ssh_box, "ssh_pass", "パスワード", "", show="*")

    # --- ネットワーク ---
    sec_net = section("ネットワーク(VM用の既定)")
    field(sec_net, "subnet", "サブネット", "192.168.11.0/24")
    field(sec_net, "gateway", "ゲートウェイ", "192.168.11.1")
    field(sec_net, "vm_range", "VMのIP範囲", "100-199",
          hint="第4オクテットの範囲。空きIP検出に使用")

    # --- DNS(任意) ---
    sec_dns = section("LAN内DNS自動登録(任意)")
    dns_on = tk.BooleanVar(value=False)
    entries["dns_enabled"] = dns_on
    tk.Checkbutton(sec_dns, text="使う(PowerDNS等。未使用ならオフのままでOK)",
                   variable=dns_on, bg=_CARD, fg=_FG, selectcolor=_FIELD,
                   font=base, anchor="w", command=lambda: _toggle()).pack(fill=tk.X)
    dns_box = tk.Frame(sec_dns, bg=_CARD)
    dns_box.pack(fill=tk.X)
    field(dns_box, "dns_host", "DNSサーバーIP", "192.168.11.254")
    field(dns_box, "dns_domain", "ドメイン", "example.com")
    field(dns_box, "dns_user", "SSHユーザー", "")
    field(dns_box, "dns_pass", "SSHパスワード", "", show="*")

    # --- CurseForge(任意) ---
    sec_cf = section("CurseForge APIキー(任意・Mod導入で使用)")
    field(sec_cf, "cf_key", "APIキー", "",
          hint="console.curseforge.com で無料発行。空でもModrinthは使えます")

    def _toggle():
        st_ssh = tk.NORMAL if mode_var.get() == "ssh" else tk.DISABLED
        for w in ssh_box.winfo_children():
            for c in w.winfo_children():
                if isinstance(c, tk.Entry):
                    c.configure(state=st_ssh)
        st_dns = tk.NORMAL if dns_on.get() else tk.DISABLED
        for w in dns_box.winfo_children():
            for c in w.winfo_children():
                if isinstance(c, tk.Entry):
                    c.configure(state=st_dns)

    _toggle()

    bar = tk.Frame(root, bg=_BG)
    bar.pack(fill=tk.X, padx=18, pady=12)

    def create():
        v = {k: (var.get() if hasattr(var, "get") else var)
             for k, var in entries.items()}
        # 軽い検証
        if v["mode"] == "ssh" and not v["ssh_host"].strip():
            messagebox.showwarning("入力", "SSHモードではホストIPが必要です", parent=root)
            return
        for k in ("subnet", "gateway", "vm_range"):
            if not v[k].strip():
                messagebox.showwarning("入力", "ネットワーク項目を埋めてください", parent=root)
                return
        text = _build_yaml(v)
        try:
            config_path.write_text(text, encoding="utf-8")
            # 生成物が読めるか検証
            from core.config import load_config
            load_config(config_path)
        except Exception as exc:
            try:
                config_path.unlink()
            except OSError:
                pass
            messagebox.showerror("作成失敗",
                                 f"config.yaml の生成に失敗しました:\n{exc}", parent=root)
            return
        result["ok"] = True
        root.destroy()

    def cancel():
        result["ok"] = False
        root.destroy()

    tk.Button(bar, text="✔ 作成して起動", command=create, bg=_OK, fg="#ffffff",
              font=h2, relief=tk.FLAT, padx=16, pady=8, cursor="hand2"
              ).pack(side=tk.LEFT)
    tk.Button(bar, text="キャンセル", command=cancel, bg=_CARD, fg=_MUTED,
              font=h2, relief=tk.FLAT, padx=16, pady=8, cursor="hand2"
              ).pack(side=tk.LEFT, padx=8)

    root.mainloop()
    return result["ok"]
