"""時系列の履歴(CPU/メモリ/人数)。ダッシュボードのグラフ用。

グラフ描画にmatplotlib等は使わない(exeが+30MB以上になるため)。
ここでは数値の履歴だけを持ち、描画はGUI側がCanvasに自前で描く。

リングバッファなのでメモリは一定(既定=30秒間隔×2時間ぶん=240点)。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class History:
    def __init__(self, maxlen: int = 240):
        self._lock = threading.Lock()
        self.maxlen = maxlen
        self._series: dict[str, deque] = {}

    def add(self, key: str, value: float, ts: float | None = None) -> None:
        with self._lock:
            d = self._series.get(key)
            if d is None:
                d = self._series[key] = deque(maxlen=self.maxlen)
            d.append((ts or time.time(), float(value)))

    def series(self, key: str) -> list[list[float]]:
        with self._lock:
            return [[t, v] for t, v in self._series.get(key, ())]

    def all(self) -> dict[str, list[list[float]]]:
        with self._lock:
            return {k: [[t, v] for t, v in d] for k, d in self._series.items()}

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._series)


class HostSampler:
    """ホストのCPU/メモリを定期サンプリングして History に入れる。"""

    def __init__(self, ctx, history: History, interval: int = 30):
        self.ctx = ctx
        self.history = history
        self.interval = interval
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="gsm-sampler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:
                print("リソース採取で例外:", exc)
            self._stop.wait(self.interval)

    def sample(self) -> None:
        # Get-Counter はカウンタ名が日本語ロケールで変わるのでWMIクラス名で取る
        r = self.ctx.runner.run_ps(
            "$c=(Get-CimInstance Win32_Processor | Measure-Object -Property "
            "LoadPercentage -Average).Average; "
            "$o=Get-CimInstance Win32_OperatingSystem; "
            '"$c|$($o.FreePhysicalMemory)|$($o.TotalVisibleMemorySize)"', timeout=20)
        out = (r.stdout or "").strip()
        if "|" not in out:
            return
        cpu, free_kb, total_kb = out.split("|")
        try:
            cpu_v = float(cpu or 0)
            used_gb = (int(total_kb) - int(free_kb)) / 1024 / 1024
            total_gb = int(total_kb) / 1024 / 1024
        except ValueError:
            return
        now = time.time()
        self.history.add("cpu", cpu_v, now)
        self.history.add("mem_used_gb", round(used_gb, 2), now)
        self.history.add("mem_total_gb", round(total_gb, 2), now)
