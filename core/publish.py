"""外部公開ヘルスチェック(FQDNで外部から到達できるかの確認 + 自宅側の自動追随)。

WAN IP変動時:
  - 自宅DNSの公開用レコード(public_name)はアプリが自動更新する。
  - レジストラ側のglue(NSのIP)は手動更新が必要 → 世界からの解決結果と現WANを突き合わせ、
    ズレていたら「お名前.comで◯◯を△△に変更」という具体的な手順を返す。
外部視点の確認は公開DoHリゾルバ(Google)を使う(アプリはLAN内にあるため)。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

from . import dnsreg, upnp
from .config import AppConfig

DOH_URL = "https://dns.google/resolve"


@dataclass
class PublishHealth:
    wan_ip: str | None                 # UPnPで得た現在の本当のWAN IP
    external_ip: str | None            # 世界(DoH)から見たpublic_nameの解決先
    status: str                        # "ok" / "propagating" / "unreachable" / "error"
    needs_action: bool                 # ユーザーの手動対応が必要か
    message: str                       # 1行サマリ
    instructions: str = ""             # 手動対応の具体手順(needs_action時)
    synced: list[str] = field(default_factory=list)  # 自動更新した自宅レコード


def get_wan_ip(gateway: str) -> str | None:
    try:
        gw = upnp.find_gateway(bind_ip=upnp.local_ip_toward(gateway),
                               prefer_host=gateway)
        return gw.external_ip
    except Exception:
        return None


def external_resolve(name: str, rtype: str = "A", timeout: float = 8.0) -> str | None:
    """公開DoHリゾルバで名前を解決し、最初のIPを返す(世界から見た値)。"""
    url = f"{DOH_URL}?name={name}&type={rtype}"
    req = urllib.request.Request(url, headers={"User-Agent": "game-server-manager"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    if data.get("Status") != 0:
        return None
    for ans in data.get("Answer", []):
        if ans.get("type") == 1:  # A
            return ans.get("data")
    return None


def check_and_sync(cfg: AppConfig, auto_sync: bool = True,
                   progress=lambda t: None) -> PublishHealth:
    """外部公開の状態を確認し、自宅側レコードは自動追随、glueズレは手順を返す。"""
    pub = cfg.publish
    if pub is None:
        return PublishHealth(None, None, "error", False,
                             "publish設定がありません(config.yaml)")

    wan = get_wan_ip(cfg.network.gateway)
    if not wan:
        return PublishHealth(None, None, "error", False,
                             "ルーターからWAN IPを取得できませんでした(UPnP不可?)")

    # 1. WAN IP変動の自動追随(自宅側)
    synced: list[str] = []
    if auto_sync and cfg.dns is not None:
        try:
            # 前回WANと変わっていたら、旧WANを指す全Aレコードを一括で新WANへ
            if pub.last_wan_ip and pub.last_wan_ip != wan:
                dnsreg.replace_a_ip(cfg.dns, pub.last_wan_ip, wan, progress=progress)
                synced.append(f"旧WAN {pub.last_wan_ip}→{wan} の全レコード")
            # 公開用レコードがWANを指していなければ補正(初回・手動ズレ対策)
            elif external_resolve(pub.public_name) != wan:
                dnsreg.set_a_record(cfg.dns, pub.public_name, wan, progress=progress)
                synced.append(pub.public_name)
        except Exception as exc:
            progress(f"自宅レコード更新でエラー: {exc}")

    # 2. 世界から見た解決結果と現WANを突き合わせる
    seen = external_resolve(pub.public_name)

    if seen == wan:
        return PublishHealth(
            wan, seen, "ok", False,
            f"✅ 外部公開OK: {pub.public_name} → {wan}(世界から到達可能)",
            synced=synced)

    # ズレている/引けない → glueが古い可能性が高い
    hosts = "\n".join(f"    - {h}" for h in pub.glue_hosts)
    instr = (
        f"WAN IPが変わったため、レジストラ側のネームサーバーIP(glue)の更新が必要です。\n\n"
        f"【{pub.registrar or 'ドメインレジストラ'}】で、以下のホストのIPアドレスを\n"
        f"  新しいWAN IP: {wan}\n"
        f"に変更してください:\n{hosts}\n\n"
        f"※ 自宅DNSの公開レコード({pub.public_name})はアプリが自動更新済みです。\n"
        f"※ 反映には数時間かかることがあります(glueのTTLのため)。")

    if seen is None:
        return PublishHealth(
            wan, None, "unreachable", True,
            f"⚠️ 外部から {pub.public_name} を解決できません(WAN IP={wan})",
            instructions=instr, synced=synced)
    return PublishHealth(
        wan, seen, "propagating", True,
        f"⏳ 世界は {seen} を見ています(現在のWANは {wan})",
        instructions=instr, synced=synced)
