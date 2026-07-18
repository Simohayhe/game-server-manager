"""自作ウィジェット(グラフ / 統計カード / ログビュー)。

グラフは matplotlib を使わず Canvas に自前で描く。
  - exeが+30MB以上にならない
  - 見た目をアプリの世界観に合わせられる(既製品の"どこかで見た感"を避ける)
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

# 配色(customtkinterのダークに合わせる)
BG = "#1a1d23"
CARD = "#22262e"
LINE = "#2f353f"
TEXT = "#e8eaed"
MUTED = "#8a93a0"
ACCENT = "#5b9dff"
OK = "#3fd07a"
WARN = "#ffb454"
ERR = "#ff6b6b"


class Sparkline(ctk.CTkFrame):
    """小さな折れ線グラフ。時系列 [[ts, value], ...] を描く。

    ・y軸は自動スケール(最大値を切りの良い値に丸める)
    ・面塗り+線+最新値の点、右上に現在値
    """

    def __init__(self, master, title: str, unit: str = "", color: str = ACCENT,
                 height: int = 120, y_max: float | None = None, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=10, **kw)
        self.title_text = title
        self.unit = unit
        self.color = color
        self.y_max_fixed = y_max
        self._data: list[list[float]] = []

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(head, text=title, text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.value_lb = ctk.CTkLabel(head, text="—", text_color=TEXT,
                                     font=ctk.CTkFont(size=16, weight="bold"))
        self.value_lb.pack(side="right")

        self.canvas = tk.Canvas(self, height=height, bg=CARD, highlightthickness=0,
                                bd=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(4, 10))
        self.canvas.bind("<Configure>", lambda _e: self._render())

    def set_data(self, data: list[list[float]]) -> None:
        self._data = data or []
        self._render()

    def _nice_max(self, v: float) -> float:
        if self.y_max_fixed:
            return self.y_max_fixed
        if v <= 0:
            return 1.0
        for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
            if v <= step:
                return float(step)
        return float(int(v / 100 + 1) * 100)

    def _render(self) -> None:
        # 注: CTkFrame が内部で _draw() を使うので、自前の描画は別名にする
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 2 or h <= 2:
            return
        pad_l, pad_r, pad_t, pad_b = 6, 6, 8, 6
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        if not self._data:
            c.create_text(w // 2, h // 2, text="データ取得中…", fill=MUTED,
                          font=("", 10))
            return
        vals = [v for _t, v in self._data]
        ymax = self._nice_max(max(vals))
        # 目盛り線
        for frac in (0, 0.5, 1.0):
            y = pad_t + gh * frac
            c.create_line(pad_l, y, w - pad_r, y, fill=LINE)
        # 折れ線
        n = len(self._data)
        pts = []
        for i, (_t, v) in enumerate(self._data):
            x = pad_l + (gw * i / max(1, n - 1))
            y = pad_t + gh * (1 - min(v, ymax) / ymax)
            pts.append((x, y))
        if len(pts) >= 2:
            fill = [(pad_l, pad_t + gh)] + pts + [(pts[-1][0], pad_t + gh)]
            c.create_polygon([co for p in fill for co in p],
                             fill=self._dim(self.color), outline="")
            c.create_line([co for p in pts for co in p], fill=self.color, width=2,
                          smooth=True)
        x, y = pts[-1]
        c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=self.color, outline=CARD,
                      width=2)
        self.value_lb.configure(text=f"{vals[-1]:.0f}{self.unit}"
                                if ymax >= 10 else f"{vals[-1]:.1f}{self.unit}")

    @staticmethod
    def _dim(hex_color: str) -> str:
        """線色を暗くして面塗りに使う(半透明が使えないので合成する)。"""
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        br, bg_, bb = int(CARD[1:3], 16), int(CARD[3:5], 16), int(CARD[5:7], 16)
        a = 0.22
        return "#%02x%02x%02x" % (int(r * a + br * (1 - a)),
                                  int(g * a + bg_ * (1 - a)),
                                  int(b * a + bb * (1 - a)))


class StatCard(ctk.CTkFrame):
    """1つの数値を大きく見せるカード。ダッシュボード用。"""

    def __init__(self, master, label: str, value: str = "—", sub: str = "",
                 color: str = TEXT, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=10, **kw)
        ctk.CTkLabel(self, text=label, text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=14, pady=(10, 0))
        self.value_lb = ctk.CTkLabel(self, text=value, text_color=color,
                                     font=ctk.CTkFont(size=26, weight="bold"))
        self.value_lb.pack(anchor="w", padx=14, pady=(0, 0))
        self.sub_lb = ctk.CTkLabel(self, text=sub, text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.sub_lb.pack(anchor="w", padx=14, pady=(0, 10))

    def set(self, value: str, sub: str | None = None, color: str | None = None) -> None:
        self.value_lb.configure(text=value)
        if color:
            self.value_lb.configure(text_color=color)
        if sub is not None:
            self.sub_lb.configure(text=sub)


class ServerCard(ctk.CTkFrame):
    """サーバー1台の状態カード(ダッシュボード用)。クリックで詳細へ飛べる。"""

    def __init__(self, master, name: str, icon: str = "", on_click=None, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=10, **kw)
        self.on_click = on_click
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 0))
        ctk.CTkLabel(top, text=f"{icon} {name}", text_color=TEXT, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.dot = ctk.CTkLabel(top, text="●", text_color=MUTED,
                                font=ctk.CTkFont(size=14))
        self.dot.pack(side="right")
        self.state_lb = ctk.CTkLabel(self, text="—", text_color=MUTED, anchor="w",
                                     font=ctk.CTkFont(size=12))
        self.state_lb.pack(fill="x", padx=14, pady=(2, 12))
        for w in (self, top, self.state_lb):
            w.bind("<Button-1>", self._click)

    def _click(self, _e=None) -> None:
        if self.on_click:
            self.on_click()

    def set(self, running: bool | None, detail: str, starting: bool = False) -> None:
        if starting:                       # プロセスは居るが起動完了前(ARKの advertising 待ち)
            color = WARN
        else:
            color = OK if running else (MUTED if running is not None else WARN)
        self.dot.configure(text_color=color)
        self.state_lb.configure(text=detail)


class LogView(ctk.CTkFrame):
    """ライブログ表示。追尾(自動スクロール)ON/OFF付き。

    もっさり対策(指摘を受けて改善):
      旧: 2〜3秒ごとに 全消去 → 250行を全挿入。毎回チラつき、スクロールも飛んでいた。
      新: 増えた行だけ末尾に追記する。変化が無ければウィジェットに触れない。
          表示部品も CTkTextbox(内部でtk.Textをラップ=オーバーヘッド有) をやめて
          素の tk.Text にした(スクロールと描画が明確に速い)。
    """

    MAX_LINES = 2000          # 溜まりすぎたら古い行を捨てる(メモリと描画の保険)

    def __init__(self, master, fetch_fn, worker, interval_ms: int = 700, **kw):
        """fetch_fn(offset) -> callable が {log, offset, append} を返すと差分モード。

        体感の遅さは更新間隔がほぼ全て(実測: API往復13ms / 旧間隔2000ms)。
        差分取得(since=バイト位置)で1往復を小さくしたうえで、間隔を700msに詰めた。

        ※ ロングポーリング(サーバー側で増えるまで待つ)を試したが逆に遅くなったので不採用。
          worker が1本のスレッドで待機リクエストがそれを最大10秒占有するため、
          対象を切り替えた時に新しいログの取得が後ろで順番待ちになってしまう。
        """
        super().__init__(master, fg_color="transparent", **kw)
        self.fetch_fn = fetch_fn
        self.worker = worker
        self.interval_ms = interval_ms
        self._running = False
        self._lines: list[str] = []
        self._offset = 0          # 次に読むバイト位置(差分取得用)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 6))
        self.follow = ctk.CTkSwitch(bar, text="追尾", onvalue=True, offvalue=False)
        self.follow.select()
        self.follow.pack(side="left")
        ctk.CTkButton(bar, text="クリア", width=64, height=26, corner_radius=6,
                      fg_color="#2b303a", hover_color="#39404d",
                      font=ctk.CTkFont(size=11), command=self.clear).pack(side="left",
                                                                          padx=8)
        self.status = ctk.CTkLabel(bar, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        self.status.pack(side="right")

        wrap = ctk.CTkFrame(self, fg_color="#12151a", corner_radius=8)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, bg="#12151a", fg="#c9d1d9", wrap="none",
                            relief="flat", borderwidth=0, padx=10, pady=8,
                            font=("Consolas", 11), insertbackground="#c9d1d9",
                            state="disabled")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        sb.pack(side="right", fill="y", pady=2, padx=(0, 2))
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.text.xview)
        hsb.pack(side="bottom", fill="x", padx=2, pady=(0, 2))   # 長い行を読むため
        self.text.pack(fill="both", expand=True, side="left", padx=(2, 0), pady=2)
        self.text.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)

    def clear(self) -> None:
        self._lines = []
        self._offset = 0            # 次回は全文を取り直す
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._tick()

    def stop(self) -> None:
        self._running = False

    def retarget(self) -> None:
        """表示対象(サーバー)が変わった時に呼ぶ。前のログを消して取り直す。"""
        self.clear()
        if self._running:
            self._tick()

    def _tick(self) -> None:
        if not self._running or not self.winfo_exists():
            return

        def done(result, error):
            if not self._running or not self.winfo_exists():
                return
            if error is None and result is not None:
                try:
                    self._consume(result)
                except Exception as exc:
                    self.status.configure(text=f"表示エラー: {exc}")
            elif error is not None:
                self.status.configure(text=f"取得失敗: {error}")
            self.after(self.interval_ms, self._tick)
        fn = self.fetch_fn(self._offset)
        if fn is None:                    # 対象未選択 → 何もしない
            self.after(self.interval_ms, self._tick)
            return
        self.worker.submit(fn, done)

    def _consume(self, result) -> None:
        """APIの戻り(差分dict または 全文str)を取り込む。"""
        if isinstance(result, dict):
            text = result.get("log") or ""
            self._offset = result.get("offset") or 0
            if result.get("append"):       # 増分だけ届いた
                if text.strip():
                    self._append_text(text)
                    self.status.configure(text=time.strftime("更新 %H:%M:%S"))
                return
            self._apply(text)              # 初回/ローテート → 全文
        else:
            self._apply(str(result))
        self.status.configure(text=time.strftime("更新 %H:%M:%S"))

    def _append_text(self, text: str) -> None:
        add = [l for l in text.splitlines() if l]
        if not add:
            return
        self.text.configure(state="normal")
        self.text.insert("end", ("\n" if self._lines else "") + "\n".join(add))
        self._lines.extend(add)
        over = len(self._lines) - self.MAX_LINES
        if over > 0:
            self.text.delete("1.0", f"{over + 1}.0")
            del self._lines[:over]
        self.text.configure(state="disabled")
        if self.follow.get():
            self._scroll_end()

    def _scroll_end(self) -> None:
        """縦だけ末尾へ追尾する。

        see("end") は末尾の「行末」を見せようとするため、wrap="none" だと
        長い行(ARKの起動引数など)で右端まで横スクロールしてしまい、
        他の短い行が画面外になって「空白だらけ」に見えていた。
        """
        self.text.yview_moveto(1.0)
        self.text.xview_moveto(0)      # 横は常に行頭を見せる

    def _apply(self, text: str) -> None:
        """増えた行だけ追記する。全消去→全挿入をやめたのでチラつかない。"""
        new_lines = text.splitlines()
        if not new_lines:
            return
        add = self._delta(new_lines)
        if add is None:                  # 変化なし → ウィジェットに触れない
            return
        self.text.configure(state="normal")
        if add is False:                 # 対応が取れない(ログ切替/ローテート) → 全入替
            self.text.delete("1.0", "end")
            self.text.insert("1.0", "\n".join(new_lines))
            self._lines = list(new_lines)
        else:
            self.text.insert("end", "\n" + "\n".join(add) if self._lines
                             else "\n".join(add))
            self._lines.extend(add)
            over = len(self._lines) - self.MAX_LINES
            if over > 0:                 # 古い行を捨てる(描画が重くならないように)
                self.text.delete("1.0", f"{over + 1}.0")
                del self._lines[:over]
        self.text.configure(state="disabled")
        if self.follow.get():
            self._scroll_end()

    def _delta(self, new_lines: list[str]):
        """追記すべき行を返す。None=変化なし / False=全入替が必要。

        tail_log は「末尾N行」を返すので、前回の最終行が今回のどこにあるかを
        後ろから探し、それ以降を増分とする(ログ行は時刻付きでほぼ一意)。
        """
        if not self._lines:
            return new_lines
        last = self._lines[-1]
        for i in range(len(new_lines) - 1, -1, -1):
            if new_lines[i] == last:
                add = new_lines[i + 1:]
                return add if add else None
        return False                     # 見つからない=別ログに切替わった等
