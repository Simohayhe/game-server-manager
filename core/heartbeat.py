"""外部への死活信号(ハートビート)。

通知の保留・再送(core/notify.py)は「復旧後に事後報告する」仕組みで、障害の最中に
知らせることはできない。経路が死んでいる間に「経路が死んだ」と送ることは原理的に
不可能なため。

そこで逆向きにする: GSMが定期的に外部サービスへ「生きています」と送り、**信号が
途絶えたら外部サービス側が通知してくれる**(デッドマンスイッチ)。家のネットや
ルーターやPCごと落ちても、あちら側は生きているのでリアルタイムで気づける。

Healthchecks.io / BetterStack / UptimeRobot(Heartbeat) などのURLをそのまま使える。
設定は config.yaml:

    monitoring:
      heartbeat_url: https://hc-ping.com/xxxxxxxx
      interval_min: 5        # 送る間隔(外部側の猶予はこれより長めに設定する)

未設定なら何もしない(既定で無効)。送信は失敗しても本体に影響させない。
"""
from __future__ import annotations

import threading
import urllib.request

DEFAULT_INTERVAL_MIN = 5
TIMEOUT_SEC = 10


def ping(url: str, timeout: float = TIMEOUT_SEC) -> bool:
    """死活信号を1回送る。成功したらTrue(失敗は握りつぶす)。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GSM-heartbeat/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:                       # noqa: BLE001 送れない=障害中。外部側が検知する
        return False


class HeartbeatService:
    """常駐サービスに登録して使う(start/stop を持つ)。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok: bool | None = None

    @property
    def url(self) -> str:
        return str(getattr(self.ctx.config, "heartbeat_url", "") or "")

    @property
    def interval_sec(self) -> int:
        m = int(getattr(self.ctx.config, "heartbeat_interval_min", 0)
                or DEFAULT_INTERVAL_MIN)
        return max(60, m * 60)              # 短すぎる設定で叩きすぎないよう最低1分

    def start(self) -> None:
        if not self.url:
            return                          # 未設定=無効(既定)
        self._thread = threading.Thread(target=self._loop, name="gsm-heartbeat",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok = ping(self.url)
            if ok != self.last_ok:          # 状態が変わった時だけ出力(ログを汚さない)
                print("死活信号:", "送信OK" if ok else "送信できず(障害中?)")
                self.last_ok = ok
            self._stop.wait(self.interval_sec)
