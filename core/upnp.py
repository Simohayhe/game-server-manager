"""UPnPポート開放(前作PortForwardManagerのupnp.pyを駆動する薄いアダプタ)。

実処理は core/pfm_upnp.py(github.com/Simohayhe/port-forward-manager から取り込み)。
このモジュールはゲームサーバーマネージャー向けに:
  - 複数IGDが応答するこのLANで「実際にNATしている親機(グローバルIP保持)」を選ぶ
  - GUIが使いやすいAPI(find_gateway / add_mapping / delete_mapping / get_mapping)
を足すだけ。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from . import pfm_upnp

UpnpError = pfm_upnp.UPnPError


def local_ip_toward(target: str = "192.168.11.1") -> str:
    """targetへ到達する際に使うこのマシンのローカルIPを返す。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    finally:
        s.close()


@dataclass
class Gateway:
    client: "pfm_upnp.IGDClient"
    host_ip: str
    service_type: str
    control_url: str
    external_ip: str | None = None


def find_gateway(bind_ip: str | None = None,
                 prefer_host: str | None = None) -> Gateway:
    """実際にNATしている(グローバルIPを持つ)IGDを1つ返す。

    bind_ip: 互換のため受け取るが未使用(pfm_upnpが自身でNICを選ぶ)。
    prefer_host: このLAN IP(例 '192.168.11.1')のデバイスを優先。
    """
    devices = pfm_upnp.discover_devices()
    if not devices:
        raise UpnpError("UPnP対応ルーターが見つかりません(UPnPが無効の可能性)")

    cands: list[tuple[dict, "pfm_upnp.IGDClient", str]] = []
    for dev in devices:
        client = pfm_upnp.IGDClient(dev["control_url"], dev["service_type"])
        host = urlparse(dev.get("location", "")).hostname or ""
        cands.append((dev, client, host))

    def make(dev, client, host, ip):
        return Gateway(client=client, host_ip=host,
                       service_type=dev["service_type"],
                       control_url=dev["control_url"], external_ip=ip)

    # 優先ホスト(親機)を先に試す
    if prefer_host:
        for dev, client, host in cands:
            if host == prefer_host:
                try:
                    ip = client.get_external_ip()
                except UpnpError:
                    ip = None
                if ip:
                    return make(dev, client, host, ip)

    # グローバルIPを返すデバイス=実際の親機
    for dev, client, host in cands:
        try:
            ip = client.get_external_ip()
        except UpnpError:
            continue
        if ip:
            try:
                if not ipaddress.ip_address(ip).is_private:
                    return make(dev, client, host, ip)
            except ValueError:
                continue
    raise UpnpError("グローバルIPを持つルーターが見つかりません(ブリッジ動作?)")


def add_mapping(gw: Gateway, external_port: int, internal_ip: str,
                internal_port: int, protocol: str = "TCP",
                description: str = "game-server-manager", lease: int = 0) -> None:
    gw.client.add_port_mapping(external_port, internal_port, internal_ip,
                               protocol=protocol, description=description,
                               lease_duration=lease)


def delete_mapping(gw: Gateway, external_port: int, protocol: str = "TCP") -> None:
    gw.client.delete_port_mapping(external_port, protocol=protocol)


def get_mapping(gw: Gateway, external_port: int,
                protocol: str = "TCP") -> dict | None:
    """指定外部ポートのマッピングを一覧から探す。無ければNone。"""
    for m in gw.client.list_port_mappings():
        if (str(m.get("external_port")) == str(external_port)
                and (m.get("protocol") or "").upper() == protocol.upper()):
            return {
                "internal_ip": m.get("internal_client", ""),
                "internal_port": int(m.get("internal_port") or 0),
                "description": m.get("description", ""),
                "protocol": protocol,
                "external_port": external_port,
            }
    return None


def _external_ip(gw: Gateway) -> str | None:
    if gw.external_ip:
        return gw.external_ip
    try:
        return gw.client.get_external_ip()
    except UpnpError:
        return None
