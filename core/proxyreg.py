"""前段プロキシ(Velocity等)のFQDN発行。

プロキシは「外から繋ぐ唯一の入口」なので、増やすたびに接続用の名前が要る。
手で登録すると付け忘れ・WAN IP変動での取り残しが起きるため、config の
`proxies:` に書いた分だけGSMがAレコード(→WAN IP)を用意する。

ポート転送側は service/portsync_svc.py が同じ `proxies:` を見て維持する。
WAN IPが変わった時は core/publish.py の replace_a_ip が旧WANを指すAを一括で
付け替えるので、ここで作ったレコードも自動で追随する。
"""
from __future__ import annotations

from . import dnsreg


def ensure_records(cfg, wan_ip: str, progress=lambda t: None) -> list[dict]:
    """proxies の fqdn を wan_ip に向ける。既に正しければ何もしない。

    戻り値は各プロキシの結果(表示・API用)。DNS未設定やfqdn未指定はスキップ。
    """
    out = []
    dns = getattr(cfg, "dns", None)
    proxies = getattr(cfg, "proxies", None) or []
    if not proxies:
        return out
    if not dns:
        return [{"name": p.name, "fqdn": p.fqdn, "ok": False,
                 "reason": "DNS未設定"} for p in proxies if p.fqdn]
    if not wan_ip:
        return [{"name": p.name, "fqdn": p.fqdn, "ok": False,
                 "reason": "WAN IP不明"} for p in proxies if p.fqdn]

    for p in proxies:
        if not p.fqdn:
            out.append({"name": p.name, "fqdn": "", "ok": False,
                        "reason": "fqdn未指定(発行しない)"})
            continue
        try:
            dnsreg.set_a_record(dns, p.fqdn, wan_ip, progress=progress)
            out.append({"name": p.name, "fqdn": p.fqdn, "ip": wan_ip, "ok": True})
        except Exception as exc:              # noqa: BLE001 1件失敗で全体を止めない
            out.append({"name": p.name, "fqdn": p.fqdn, "ok": False,
                        "reason": str(exc)})
    return out


def summary(cfg) -> list[dict]:
    """設定済みプロキシの一覧(接続先の案内用)。"""
    return [{
        "name": p.name, "ip": p.ip, "port": p.port, "proto": p.proto,
        "fqdn": p.fqdn, "game": p.game,
        # 25565はMinecraftの既定ポートなので、その時だけポート指定が要らない
        "connect": (p.fqdn or p.ip) + ("" if p.port == 25565 else f":{p.port}"),
    } for p in (getattr(cfg, "proxies", None) or [])]
