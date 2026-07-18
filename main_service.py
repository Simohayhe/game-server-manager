"""GSM 常駐サービスのエントリポイント。

    python main_service.py

GUIとは独立して動き、予約・バックアップ・動的設定配信・監視を担当する。
PCログオン時に自動起動させる想定(P4)。GUI(main_ark.py 等)はこのサービスの
ローカルHTTP APIを叩くだけのクライアントになる。
"""
from __future__ import annotations

import argparse
import sys

from service.api import API_PORT_DEFAULT, ApiServer
from service.app import Service
from service.context import Context
from service.dynserve import DynServe
from service.history import History, HostSampler
from service.monitor import Monitor
from service.portsync_svc import PortSyncService
from service.recovery import RecoveryService
from service.routes import build_router
from service.scheduler import SchedulerService
from service.state import StateCache


def build_service(api_port: int = API_PORT_DEFAULT) -> Service:
    ctx = Context()
    state = StateCache()
    svc = Service(ctx)

    notifier = _make_notifier()
    hist = History()
    sampler = HostSampler(ctx, hist)
    dyn = DynServe(ctx)
    ports = PortSyncService(ctx, state, notifier=notifier)
    rec = RecoveryService(ctx, notifier=notifier, portsync=ports)
    sched = SchedulerService(ctx, state=state, notifier=notifier, recovery=rec)
    mon = Monitor(ctx, state, notifier=notifier, on_change=rec.on_change,
                  on_ready=rec.on_ready, history=hist)
    api = ApiServer(build_router(ctx, state, scheduler=sched, dynserve=dyn,
                                 portsync=ports, recovery=rec, history=hist,
                                 notifier=notifier),
                    port=api_port)

    # 起動順: 配信 → 監視 → 予約 → ポート同期 → 採取 → API(最後=全部揃ってから受付)
    for c in (dyn, mon, sched, ports, sampler, api):
        svc.add(c)
    svc.ctx_extra = {"state": state, "dyn": dyn, "sched": sched, "api": api,
                     "ports": ports, "rec": rec, "history": hist}
    return svc


def _make_notifier():
    """Discord通知。設定が無ければ何もしない。複数送信先に対応。

    送信先ごとに「何を通知するか」が違うので、イベントを受け取る送信先すべてに投げる。
    notify.json はGUIから編集されるので、変更を即反映するため mtime を見て読み直す。
    送信はネットワーク待ちがあるので、呼び出し側を止めないよう別スレッドで投げる。
    """
    from core import notify
    from core.paths import app_dir
    path = app_dir() / "notify.json"
    cache = {"mtime": None, "cfg": notify.NotifyConfig()}

    def current():
        try:
            m = path.stat().st_mtime
        except OSError:
            return notify.NotifyConfig()
        if m != cache["mtime"]:
            try:
                cache["cfg"] = notify.load(path)
                cache["mtime"] = m
            except Exception as exc:
                print("通知設定の読み込みに失敗(前回の設定で続行):", exc)
        return cache["cfg"]

    def send(event: str, text: str, game: str | None = None) -> None:
        import threading
        for dest in current().targets(event, game):     # イベント＋ゲームで絞る
            url = dest.webhook_url
            threading.Thread(
                target=lambda u=url: _safe_send(notify, u, text),
                daemon=True).start()
    return send


def _safe_send(notify, url: str, text: str) -> None:
    try:
        notify.send(url, text)
    except Exception as exc:
        print("通知の送信に失敗:", exc)


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM 常駐サービス")
    ap.add_argument("--port", type=int, default=API_PORT_DEFAULT,
                    help=f"APIポート(既定 {API_PORT_DEFAULT})")
    args = ap.parse_args()
    try:
        svc = build_service(args.port)
    except Exception as exc:
        print(f"起動に失敗しました: {exc}")
        return 1
    ctx = svc.ctx
    print(f"ARK {len(ctx.arkhosts)}マップ / サーバー {len(ctx.servers)}台 を認識")
    print(f"API: http://127.0.0.1:{args.port}")
    svc.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
