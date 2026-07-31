"""ポート競合の検出。

IP競合は監視が見ているが、ポートの重複は誰も見ていなかった。特に危ないのは:

- **ARKは全マップが同じホスト上で動く**。Port/Queryport/RCONPort のどれかが
  被ると、後から起動した方が待受けできずに起動失敗する(原因が分かりにくい)。
- ホストにはGSM自身(API/Web/syslog)・VPN・プロキシも同居している。ARKのポートを
  増やすときにこれらと衝突しうる。
- WAN側(ルーターの転送)は**外部ポートが全サービスで一意**でなければならない。
  被ると片方の転送が上書きされ、意図しないサーバーに繋がる。

VMは1台ごとにIPが別なので、VM内の 25565 同士は競合しない(同じIPの中だけで見る)。
"""
from __future__ import annotations


def _add(seen: dict, key: tuple, owner: str) -> None:
    seen.setdefault(key, []).append(owner)


def collect(cfg, host_ip: str = "") -> dict:
    """設定から「同じ待受先で使うポート」と「WAN外部ポート」を集める。

    戻り値: {"local": {(ip, port, proto): [所有者,…]}, "wan": {(port, proto): [所有者,…]}}
    """
    local: dict = {}
    wan: dict = {}
    host = host_ip or "HOST"

    # GSM自身(ホストで待受)
    api_host = getattr(cfg, "api_host", "127.0.0.1")
    _add(local, (host, 8770, "TCP"), "GSM API")
    web = int(getattr(cfg, "api_web_port", 0) or 0)
    if web:
        _add(local, (host, web, "TCP"), "GSM Web UI")
    sl = getattr(cfg, "syslog", None)
    if sl and sl.enabled:
        _add(local, (host, sl.port, "UDP"), "syslog受信")
    del api_host

    # ARK: 全マップがホスト上。3種類のポートを持つ
    for a in (getattr(cfg, "ark_hosts", None) or []):
        label = a.display_name or a.map_label
        for port, kind in ((a.game_port, "ゲーム"), (a.query_port, "クエリ")):
            if port:
                _add(local, (host, int(port), "UDP"), f"ARK {label}({kind})")
                _add(wan, (int(port), "UDP"), f"ARK {label}({kind})")
        if a.rcon_port_arg:                     # RCONは外に出さない(ローカルのみ)
            _add(local, (host, int(a.rcon_port_arg), "TCP"), f"ARK {label}(RCON)")

    # 前段プロキシ(自分のホストで待受)
    for p in (getattr(cfg, "proxies", None) or []):
        _add(local, (p.ip, int(p.port), p.proto), f"プロキシ {p.name}")
        _add(wan, (int(p.port), p.proto), f"プロキシ {p.name}")

    # MC/Palworld(それぞれ別VM。VM内の重複と、WAN外部ポートの重複を見る)
    for s in (getattr(cfg, "servers", None) or []):
        proto = "UDP" if s.game == "palworld" else "TCP"
        if s.game_port:
            _add(local, (s.address, int(s.game_port), proto), f"{s.display_name}(ゲーム)")
        if s.rcon:
            _add(local, (s.address, int(s.rcon.port), "TCP"), f"{s.display_name}(RCON)")
        # proxied はプロキシ経由なので外部公開しない=WAN側には出ない
        if s.external_port and not getattr(s, "proxied", False):
            _add(wan, (int(s.external_port), proto), f"{s.display_name}(外部)")

    # 追加のポート転送(portsync.extra)
    for e in (getattr(cfg, "portsync_extra", None) or []):
        try:
            _add(wan, (int(e["ext_port"]), str(e.get("proto", "TCP")).upper()),
                 f"追加転送 {e.get('label', '?')}")
        except (KeyError, TypeError, ValueError):
            continue

    return {"local": local, "wan": wan}


def find_conflicts(cfg, host_ip: str = "") -> list[dict]:
    """競合(同じ待受先/同じWANポートを2つ以上が使う)を返す。無ければ空。"""
    data = collect(cfg, host_ip)
    out = []
    for (ip, port, proto), owners in sorted(data["local"].items()):
        if len(owners) > 1:
            out.append({"scope": "local", "where": ip, "port": port,
                        "proto": proto, "owners": owners})
    for (port, proto), owners in sorted(data["wan"].items()):
        if len(owners) > 1:
            out.append({"scope": "wan", "where": "WAN(ルーター)", "port": port,
                        "proto": proto, "owners": owners})
    return out


def describe(conflicts: list[dict]) -> str:
    """通知/表示用の文字列。"""
    if not conflicts:
        return "ポート競合はありません"
    lines = []
    for c in conflicts:
        lines.append(f"{c['where']} {c['proto']}/{c['port']} … "
                     + " と ".join(c["owners"]))
    return "⚠ ポート競合: " + " / ".join(lines)
