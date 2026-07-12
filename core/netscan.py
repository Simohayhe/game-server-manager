"""LAN上のIP使用状況スキャン(クローン時のIP選択・競合検知用)。"""
from __future__ import annotations

import platform
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

_IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0


def ping_alive(ip: str, timeout_ms: int = 600) -> bool:
    """ICMP pingに応答があればTrue。"""
    if _IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        r = subprocess.run(cmd, capture_output=True,
                           creationflags=_NO_WINDOW, timeout=timeout_ms / 1000 + 2)
        if r.returncode != 0:
            return False
        # Windowsのpingは「宛先に到達できません」でも終了コード0になるため応答元を確認
        return b"TTL=" in r.stdout.upper() or b"ttl=" in r.stdout
    except Exception:
        return False


def tcp_alive(ip: str, port: int = 22, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def ip_in_use(ip: str) -> bool:
    """使用中らしければTrue(ping応答 or SSHポート応答)。"""
    return ping_alive(ip) or tcp_alive(ip)


_ARP_RE = None


def arp_table() -> dict[str, str]:
    """ARPテーブルを読み、MAC(区切りなし大文字) → IPv4 の辞書を返す。"""
    global _ARP_RE
    import re
    if _ARP_RE is None:
        _ARP_RE = re.compile(
            r"(\d+\.\d+\.\d+\.\d+)\s+((?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})")
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                           creationflags=_NO_WINDOW, timeout=10)
    except Exception:
        return {}
    table: dict[str, str] = {}
    for ip, mac in _ARP_RE.findall(r.stdout):
        table[mac.replace("-", "").replace(":", "").upper()] = ip
    return table


def scan_used_octets(prefix: str, start: int, end: int) -> set[int]:
    """prefix(例 '192.168.11')のstart〜endを並列pingし、応答のあった第4オクテット集合を返す。"""
    octets = list(range(start, end + 1))
    with ThreadPoolExecutor(max_workers=64) as ex:
        alive = list(ex.map(lambda o: ping_alive(f"{prefix}.{o}", 500), octets))
    return {o for o, a in zip(octets, alive) if a}
