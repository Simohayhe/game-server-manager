"""常駐サービス本体。GUIとは無関係に動き続ける。

ここが動いていれば、GUIを1つも開いていなくても
  ・予約(バックアップ/更新/再起動)が発火する
  ・10分毎のプレイヤーデータバックアップが走る
  ・動的設定(交配倍率など)が配信される
という状態になる。GUIは見るだけのクライアント。
"""
from __future__ import annotations

import signal
import threading
import time

from .context import Context


class Service:
    def __init__(self, context: Context | None = None):
        self.ctx = context or Context()
        self._stop = threading.Event()
        self._components: list = []

    def add(self, component) -> None:
        """start()/stop() を持つ常駐部品(スケジューラ・監視・配信・API)を登録する。"""
        self._components.append(component)

    def start(self) -> None:
        for c in self._components:
            name = type(c).__name__
            try:
                c.start()
                self.log(f"起動: {name}")
            except Exception as exc:
                self.log(f"起動失敗: {name}: {exc}")

    def stop(self) -> None:
        self._stop.set()
        for c in reversed(self._components):
            try:
                c.stop()
            except Exception as exc:
                self.log(f"停止失敗: {type(c).__name__}: {exc}")

    def run_forever(self) -> None:
        """Ctrl-C / タスクキル まで動き続ける。"""
        self.start()
        self.log("GSMサービス稼働中(GUIを閉じても予約・バックアップは動きます)")

        def _sig(_s, _f):
            self.log("停止要求を受信")
            self._stop.set()
        try:
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)
        except (ValueError, AttributeError):
            pass                     # 別スレッド/Windowsでは登録できないことがある
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            self.stop()
            self.log("GSMサービス停止")

    @staticmethod
    def log(text: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)
