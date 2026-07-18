"""外部公開ステータスの判定(読み取り専用)。旧GUIの _compute_pubstat 相当。

判定材料: ①ルーターのUPnPポート転送が そのサーバー宛に存在するか
          ②自FQDNのA解決が現WAN IPを指すか。両方揃えば「公開中」。
ルーター/DNSを変更しない安全な処理なので監視ループから定期的に呼べる。
"""
from __future__ import annotations

from core import conntest, upnp

_gw_cache = {"gw": None}


def _gateway(ctx):
    gw = _gw_cache["gw"]
    if gw is not None:
        return gw
    prefer = ctx.config.network.gateway if ctx.config.network else None
    gw = upnp.find_gateway(prefer_host=prefer)
    _gw_cache["gw"] = gw
    return gw


def compute(ctx) -> dict:
    """{"servers": {name: 状態}, "ark": {idx: 状態}} を返す。UPnP不可なら "―"。"""
    servers = [(n, s.profile) for n, s in ctx.servers.items()
               if getattr(s.profile, "external_port", None)]
    arks = list(enumerate(ctx.arkhosts))
    if not servers and not arks:
        return {"servers": {}, "ark": {}}
    try:
        gw = _gateway(ctx)
        mappings = gw.client.list_port_mappings()
        wan = gw.external_ip
    except Exception:
        _gw_cache["gw"] = None                      # 次回に再探索
        return {"servers": {n: "―" for n, _p in servers},
                "ark": {str(i): "―" for i, _ah in arks}}

    existing = {(str(m.get("external_port")), (m.get("protocol") or "").upper()): m
                for m in mappings}
    resolver = ctx.config.dns.host if ctx.config.dns else None

    srv = {}
    for n, p in servers:
        proto = "UDP" if p.game == "palworld" else "TCP"
        m = existing.get((str(p.external_port), proto))
        forwarded = bool(m and m.get("internal_client") == p.address)
        dns_wan, dns_checked = False, False
        if resolver and p.fqdn and wan:
            dns_checked = True
            try:
                dns_wan = wan in conntest.dns_query(resolver, p.fqdn, 1)
            except Exception:
                dns_checked = False
        if forwarded and (dns_wan or not dns_checked):
            srv[n] = "🌐 公開中"
        elif forwarded or dns_wan:
            srv[n] = "🟡 要確認"
        else:
            srv[n] = "🔒 非公開"

    ark = {}
    if arks:
        try:
            host_ip = upnp.local_ip_toward(
                ctx.config.network.gateway if ctx.config.network else "192.168.11.1")
        except Exception:
            host_ip = None
        for i, ah in arks:
            gp = getattr(ah.cfg, "game_port", None)
            qp = getattr(ah.cfg, "query_port", None)
            gm = existing.get((str(gp), "UDP")) if gp else None
            qm = existing.get((str(qp), "UDP")) if qp else None
            g_ok = bool(gm and gm.get("internal_client") == host_ip)
            q_ok = bool(qm and qm.get("internal_client") == host_ip)
            if g_ok and q_ok:
                ark[str(i)] = "🌐 公開中"
            elif g_ok or q_ok:
                ark[str(i)] = "🟡 要確認"
            else:
                ark[str(i)] = "🔒 非公開"
    return {"servers": srv, "ark": ark}


def publish_server(ctx, profile) -> str:
    """MC/PalworldをFQDNで外部公開(UPnP転送 + DNS A/SRV)。WAN IPを返す。"""
    ext = profile.external_port or profile.game_port
    proto = "UDP" if profile.game == "palworld" else "TCP"
    net = ctx.config.network
    gw = upnp.find_gateway(bind_ip=upnp.local_ip_toward(net.gateway) if net else None,
                           prefer_host=net.gateway if net else None)
    wan = gw.external_ip
    upnp.add_mapping(gw, ext, profile.address, profile.game_port, proto,
                     description=f"gsm-{profile.name}")
    if profile.fqdn and ctx.config.dns is not None:
        from core import dnsreg
        if profile.game == "palworld":              # PalworldはSRV非対応=Aのみ
            dnsreg.set_a_record(ctx.config.dns, profile.fqdn, wan)
        else:
            dnsreg.publish_server(ctx.config.dns, profile.fqdn, wan, ext,
                                  service="minecraft")
    _gw_cache["gw"] = None                          # 状態が変わったので次回再取得
    return wan


def unpublish_server(ctx, profile) -> None:
    """外部公開を停止(UPnP転送削除 + SRV削除)。"""
    ext = profile.external_port or profile.game_port
    proto = "UDP" if profile.game == "palworld" else "TCP"
    net = ctx.config.network
    gw = upnp.find_gateway(bind_ip=upnp.local_ip_toward(net.gateway) if net else None,
                           prefer_host=net.gateway if net else None)
    upnp.delete_mapping(gw, ext, proto)
    if profile.fqdn and ctx.config.dns is not None and profile.game != "palworld":
        from core import dnsreg
        dnsreg.unpublish_server(ctx.config.dns, profile.fqdn, service="minecraft")
    _gw_cache["gw"] = None
