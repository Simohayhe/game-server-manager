"""ダッシュボード。開いた瞬間に全体が分かる画面。

・上段: 稼働台数 / 総プレイヤー数 / CPU / メモリ の数値カード
・中段: CPU・メモリ・プレイヤー数の推移グラフ(常駐サービスが持つ履歴)
・下段: サーバーごとの状態カード(クリックでその画面へ)
"""
from __future__ import annotations

import customtkinter as ctk

from .widgets import ACCENT, ERR, MUTED, OK, WARN, ServerCard, Sparkline, StatCard


class Dashboard(ctk.CTkScrollableFrame):
    def __init__(self, master, client, worker, on_open=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self.client = client
        self.worker = worker
        self.on_open = on_open or (lambda _k: None)
        self._cards: dict[str, ServerCard] = {}
        self._alive = True
        self._visible = False        # 表示中だけ更新する(隠れている間の無駄を無くす)
        self._build()

    def on_show(self) -> None:
        if not self._visible:
            self._visible = True
            self._tick()

    def on_hide(self) -> None:
        self._visible = False

    def _build(self) -> None:
        # 数値カード
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", pady=(0, 12))
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="stat")
        self.c_running = StatCard(row, "稼働中のサーバー", "—")
        self.c_players = StatCard(row, "接続中プレイヤー", "—")
        self.c_cpu = StatCard(row, "CPU", "—")
        self.c_mem = StatCard(row, "メモリ", "—")
        for i, c in enumerate((self.c_running, self.c_players, self.c_cpu, self.c_mem)):
            c.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0), sticky="ew")

        # グラフ
        g = ctk.CTkFrame(self, fg_color="transparent")
        g.pack(fill="x", pady=(0, 12))
        for i in range(3):
            g.grid_columnconfigure(i, weight=1, uniform="graph")
        self.g_cpu = Sparkline(g, "CPU 使用率", "%", color=ACCENT, y_max=100)
        self.g_mem = Sparkline(g, "メモリ使用量", "GB", color=WARN)
        self.g_pl = Sparkline(g, "プレイヤー数(合計)", "人", color=OK)
        for i, c in enumerate((self.g_cpu, self.g_mem, self.g_pl)):
            c.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0), sticky="ew")

        ctk.CTkLabel(self, text="サーバー", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w",
                                                                    pady=(4, 6))
        self.cards = ctk.CTkFrame(self, fg_color="transparent")
        self.cards.pack(fill="both", expand=True)
        for i in range(3):
            self.cards.grid_columnconfigure(i, weight=1, uniform="card")

    def destroy(self) -> None:
        self._alive = False
        super().destroy()

    def _tick(self) -> None:
        if not self._alive or not self._visible or not self.winfo_exists():
            return

        def job():
            return (self.client.ark(), self.client.servers(),
                    self.client.get("/api/history")["history"])

        def done(data, error):
            if not self._alive or not self._visible or not self.winfo_exists():
                return
            if error is None:
                try:
                    self._apply(*data)
                except Exception as exc:      # 描画で落ちてもポーリングは続ける
                    print("ダッシュボード描画で例外:", exc)
            self.after(5000, self._tick)
        self.worker.submit(job, done)

    def _apply(self, ark: list[dict], servers: list[dict], hist: dict) -> None:
        # ---- 数値カード ----
        running = sum(1 for a in ark if a.get("ready"))
        running += sum(1 for s in servers if s.get("status") == "active")
        total = len(ark) + len(servers)
        players = sum(a.get("player_count") or 0 for a in ark if a.get("ready"))
        self.c_running.set(str(running), f"/ {total} 台",
                           OK if running else MUTED)
        self.c_players.set(str(players), "人が接続中" if players else "誰もいません",
                           OK if players else MUTED)

        cpu = hist.get("cpu") or []
        mem = hist.get("mem_used_gb") or []
        memt = hist.get("mem_total_gb") or []
        if cpu:
            v = cpu[-1][1]
            self.c_cpu.set(f"{v:.0f}%", "ホスト",
                           ERR if v >= 90 else WARN if v >= 70 else OK)
        if mem and memt:
            u, t = mem[-1][1], memt[-1][1]
            pct = u / t * 100 if t else 0
            self.c_mem.set(f"{u:.1f} GB", f"/ {t:.1f} GB ({pct:.0f}%)",
                           ERR if pct >= 90 else WARN if pct >= 75 else OK)

        # ---- グラフ ----
        self.g_cpu.set_data(cpu)
        self.g_mem.set_data(mem)
        self.g_pl.set_data(self._sum_players(hist))

        # ---- サーバーカード ----
        want = []
        for a in ark:
            run, ready = a.get("running"), a.get("ready")
            if run and not ready:
                detail, starting = "起動中…", True
            elif run:
                detail = f"{a.get('player_count') or 0}人  •  稼働 {a.get('uptime_text')}"
                starting = False
            else:
                detail, starting = "停止中", False
            want.append((f"ark:{a['index']}", "🦖", a["display_name"], run, detail,
                         "ark", starting))
        for s in servers:
            icon = "🐑" if s["game"] == "palworld" else "🟩"
            st = s.get("status")
            want.append((f"srv:{s['name']}", icon, s["display_name"],
                         st == "active" if st else None,
                         {"active": "稼働中", "inactive": "停止中",
                          "error": "接続不可"}.get(st, "確認中"),
                         "pal" if s["game"] == "palworld" else "mc", False))
        for i, (key, icon, name, run, detail, nav, starting) in enumerate(want):
            card = self._cards.get(key)
            if card is None:
                card = ServerCard(self.cards, name, icon,
                                  on_click=lambda k=nav: self.on_open(k))
                card.grid(row=i // 3, column=i % 3, padx=(0 if i % 3 == 0 else 6, 0),
                          pady=(0, 6), sticky="ew")
                self._cards[key] = card
            card.set(run, detail, starting)

    @staticmethod
    def _sum_players(hist: dict) -> list[list[float]]:
        """各マップの人数系列を時刻ごとに合算する(全体の推移)。"""
        series = [v for k, v in hist.items() if k.startswith("players:")]
        if not series:
            return []
        n = min(len(s) for s in series)
        if n == 0:
            return []
        out = []
        for i in range(n):
            ts = series[0][i][0]
            out.append([ts, sum(s[i][1] for s in series)])
        return out
