"""ホストPC(このマシン)の再起動と、再起動後の自動復帰。

再起動すると:
  - ホストのARK(プロセス)は落ちる → GSMが「動いていたマップ」を記録し、
    サービス起動時(consume_restore)に起動し直して復帰させる。
  - VM(Palworld/MC)は Hyper-V の AutomaticStopAction=Save + AutomaticStartAction=
    StartIfRunning により、メモリ状態ごと保存→ホスト起動時に自動復帰する
    (=GSM側の処理は不要)。ここでは念のためワールド保存(save)だけ保険で送る。

安全のため: 予告(RCON) → 取り消し可能カウントダウン → ARK保存停止 → shutdown /r。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

from core.paths import app_dir

RESTORE_PATH = app_dir() / "host_restore.json"
RESTORE_MAX_AGE = 60 * 60          # これより古い復帰記録は無視(誤復帰防止)
BOOT_STAGGER_SEC = 20              # ASAは同時起動が重いので復帰は間隔を空ける


def _running_ark_maps(ctx) -> list[dict]:
    out = []
    for i, ah in enumerate(ctx.arkhosts):
        try:
            if ah.is_running():
                out.append({"idx": i, "label": ah.cfg.map_label,
                            "display": ah.cfg.display_name})
        except Exception:          # noqa: BLE001
            pass
    return out


def _warn_players(ctx, seconds: int, progress) -> None:
    msg = (f"[GSM] The server PC will RESTART in {seconds} seconds. "
           "Please log off safely.")
    for ah in ctx.arkhosts:
        try:
            if ah.is_running():
                ah.announce(msg)
        except Exception:          # noqa: BLE001
            pass
    for srv in ctx.servers.values():
        try:
            if srv.status() == "active":
                srv.announce(msg)
        except Exception:          # noqa: BLE001
            pass
    progress("プレイヤーへ再起動を予告しました")


def restart_host(ctx, delay_sec: int = 60, progress=lambda t: None,
                 is_cancelled=lambda: False) -> dict:
    """予告→カウントダウン(取消可)→ARK保存停止→PC再起動。"""
    delay_sec = max(0, int(delay_sec))
    progress("再起動の準備: プレイヤーへ予告…")
    _warn_players(ctx, delay_sec, progress)

    end = time.time() + delay_sec
    while time.time() < end:
        if is_cancelled():
            progress("キャンセルされました。再起動を中止します。")
            return {"cancelled": True}
        remain = int(end - time.time())
        progress(f"再起動まで {remain} 秒…(取り消し可能)")
        time.sleep(min(3, max(1, remain)))
    if is_cancelled():
        progress("キャンセルされました。再起動を中止します。")
        return {"cancelled": True}

    # ---- ここから確定 ----
    ark = _running_ark_maps(ctx)
    try:                            # 復帰用に「動いていたARKマップ」を記録
        RESTORE_PATH.write_text(json.dumps({
            "ts": _dt.datetime.now().timestamp(),
            "ark_maps": ark, "pending": True,
        }), encoding="utf-8")
        progress(f"復帰用に記録しました(ARK {len(ark)}マップ)")
    except Exception as exc:        # noqa: BLE001
        progress(f"復帰記録に失敗(続行): {exc}")

    for a in ark:                   # ARKはプロセスが落ちるので先に saveworld して停止
        try:
            progress(f"{a['display']}: 保存して停止…")
            ctx.arkhosts[a["idx"]].stop(progress=progress)
        except Exception as exc:    # noqa: BLE001
            progress(f"{a['display']} の停止に失敗(続行): {exc}")

    # VMは止めない(Hyper-VがSave+StartIfRunningで自動復帰)。保険でワールド保存だけ。
    for srv in ctx.servers.values():
        try:
            if srv.status() != "active":
                continue
            progress(f"{srv.profile.display_name}: ワールド保存(保険)…")
            srv.announce("[GSM] Saving world before the host restarts...")
            srv.rcon_command("save" if srv.profile.game == "palworld" else "save-all")
        except Exception:           # noqa: BLE001
            pass

    progress("PCを再起動します…")
    _do_restart(ctx)
    return {"restarting": True, "ark_maps": [a["label"] for a in ark]}


def _do_restart(ctx) -> None:
    ctx.runner.run_ps('shutdown /r /t 5 /c "GSM: restarting the server PC"',
                      timeout=30)


def cancel_restart_file() -> None:
    """(保険)残った復帰記録を消す。"""
    _clear()


def consume_restore(ctx, progress=lambda t: None) -> dict:
    """サービス起動時に呼ぶ。再起動前に動いていたARKマップを起動し直す。

    記録は一度だけ消費(先にファイルを消す)。VMはHyper-V任せなので何もしない。
    """
    if not RESTORE_PATH.exists():
        return {"restored": []}
    try:
        data = json.loads(RESTORE_PATH.read_text(encoding="utf-8"))
    except Exception:               # noqa: BLE001
        _clear()
        return {"restored": []}
    _clear()                        # 二重復帰しないよう即消す
    if not data.get("pending"):
        return {"restored": []}
    age = _dt.datetime.now().timestamp() - float(data.get("ts") or 0)
    if age > RESTORE_MAX_AGE:
        progress(f"復帰記録が古い({int(age)}秒)ため無視します")
        return {"restored": []}

    maps = data.get("ark_maps") or []
    restored = []
    if maps:
        progress(f"再起動後の復帰: ARK {len(maps)}マップを起動します")
    for a in maps:
        ah = ctx.ark_by_label(a.get("label")) if a.get("label") else None
        if ah is None and a.get("idx") is not None:
            ah = ctx.ark_by_index(a["idx"])
        if ah is None:
            continue
        try:
            if ah.is_running():
                continue
            progress(f"{ah.cfg.display_name}: 復帰起動…")
            ah.start()
            restored.append(ah.cfg.map_label)
            time.sleep(BOOT_STAGGER_SEC)   # 同時起動を避けて順番に
        except Exception as exc:    # noqa: BLE001
            progress(f"{ah.cfg.display_name} の復帰起動に失敗: {exc}")
    return {"restored": restored}


def _clear() -> None:
    try:
        RESTORE_PATH.unlink()
    except OSError:
        pass
