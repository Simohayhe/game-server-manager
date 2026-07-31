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

MC_DEFAULT_PORT = 25565    # これ以外のポートはSRVで隠す(プレイヤーはポート入力不要)


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
        return [{"name": p.name, "ok": False, "reason": "DNS未設定"} for p in proxies]
    if not wan_ip:
        return [{"name": p.name, "ok": False, "reason": "WAN IP不明"} for p in proxies]

    for p in proxies:
        fqdn = p.resolve_fqdn(dns.domain)      # 既定は <クラスタ名>.<ドメイン>
        if not fqdn:
            out.append({"name": p.name, "fqdn": "", "ok": False,
                        "reason": "cluster/fqdn どちらも未指定"})
            continue
        try:
            if p.game == "minecraft" and p.port != MC_DEFAULT_PORT:
                # 既定ポート以外はSRVも張る。これが無いとプレイヤーがポートを
                # 手入力する必要があり、逆に「この名前だけが入口」にもできない
                # (WAN IPを指す他の名前が既定ポートで刺さってしまうため)。
                dnsreg.publish_server(dns, fqdn, wan_ip, p.port, progress=progress)
            else:
                dnsreg.set_a_record(dns, fqdn, wan_ip, progress=progress)
            out.append({"name": p.name, "fqdn": fqdn, "ip": wan_ip,
                        "port": p.port, "srv": p.port != MC_DEFAULT_PORT, "ok": True})
        except Exception as exc:              # noqa: BLE001 1件失敗で全体を止めない
            out.append({"name": p.name, "fqdn": fqdn, "ok": False,
                        "reason": str(exc)})
    return out


def cleanup_for_cluster(cfg, config_path, cluster: str,
                        progress=lambda t: None) -> list[str]:
    """クラスタが消えた時、そのクラスタ用プロキシの設定/DNS/転送を片付ける。

    プロキシは「そのクラスタへの入口」なので、クラスタが無くなれば存在意義が無い。
    残しておくとポート転送とDNSだけが生き続け、後から見て何のための穴か
    分からなくなる(実際にクラスタ削除後、宙に浮いた状態が発生した)。

    戻り値は片付けたプロキシ名。DNSのPTRはWAN IPを他の名前と共有しているので
    消さない(A/SRVだけ消す)。
    """
    from . import dnsreg, settings
    removed = []
    proxies = [p for p in (getattr(cfg, "proxies", None) or [])
               if (p.cluster or "") == cluster]
    if not proxies:
        return removed
    dns = getattr(cfg, "dns", None)
    for p in proxies:
        fqdn = p.resolve_fqdn(dns.domain) if dns else ""
        if fqdn and dns:
            try:                      # A/SRVのみ削除(ip未指定=PTRは触らない)
                dnsreg.unregister_host(dns, fqdn, progress=progress)
                progress(f"DNS削除: {fqdn}")
            except Exception as exc:  # noqa: BLE001 設定の掃除は続ける
                progress(f"⚠ DNS削除に失敗({fqdn}): {exc}")
        removed.append(p.name)
    # config から proxies 該当分を落とす
    try:
        keep = [{"name": p.name, "ip": p.ip, "port": p.port, "proto": p.proto,
                 "cluster": p.cluster, "fqdn": p.fqdn, "game": p.game}
                for p in (getattr(cfg, "proxies", None) or [])
                if (p.cluster or "") != cluster]
        settings.update_config(config_path, {"proxies": keep})
        progress(f"proxies から {', '.join(removed)} を削除しました")
    except Exception as exc:          # noqa: BLE001
        progress(f"⚠ proxies の更新に失敗: {exc}")
    return removed


def summary(cfg) -> list[dict]:
    """設定済みプロキシの一覧(接続先の案内用)。"""
    domain = getattr(getattr(cfg, "dns", None), "domain", "") or ""
    out = []
    for p in (getattr(cfg, "proxies", None) or []):
        fqdn = p.resolve_fqdn(domain)
        # Minecraftは既定ポート以外でもSRVを張るのでポート入力は要らない。
        # FQDNが無い(DNS未設定)時だけIP:ポートで案内する。
        srv = (p.game == "minecraft" and p.port != MC_DEFAULT_PORT and bool(fqdn))
        if fqdn:
            connect = fqdn if (p.port == MC_DEFAULT_PORT or srv) else f"{fqdn}:{p.port}"
        else:
            connect = f"{p.ip}:{p.port}"
        out.append({
            "name": p.name, "ip": p.ip, "port": p.port, "proto": p.proto,
            "cluster": p.cluster, "fqdn": fqdn, "game": p.game,
            "srv": srv, "connect": connect,
        })
    return out
