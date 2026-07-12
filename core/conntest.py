"""接続テスト: 指定リゾルバでSRV/Aを引き、実際にMinecraftサーバーへ繋いでMOTDを確認する。

Minecraftクライアントと同じ手順(SRV→ターゲット→A→接続→Server List Ping)を、
外部の公開DNS(既定 8.8.8.8)を使って行うことで、ローカルの古いキャッシュに
影響されない「まっさらな外部プレイヤー視点」の到達確認ができる。
標準ライブラリのみ。
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass

# ---- 最小DNSクライアント(指定リゾルバに直接問い合わせる) ----

_TYPE_A = 1
_TYPE_SRV = 33


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode("ascii")
                    for p in name.rstrip(".").split(".")) + b"\x00"


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels, jumped, end = [], False, offset
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:                      # 圧縮ポインタ
            ptr = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset, jumped = ptr, True
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), (end if jumped else offset)


def dns_query(resolver: str, name: str, qtype: int, timeout: float = 5.0) -> list:
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)  # RD=1
    pkt = header + _encode_name(name) + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (resolver, 53))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    qd, an = struct.unpack(">HH", data[4:8])
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    out = []
    for _ in range(an):
        _, off = _read_name(data, off)
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == _TYPE_A and qtype == _TYPE_A:
            out.append(".".join(str(b) for b in data[off:off + 4]))
        elif rtype == _TYPE_SRV and qtype == _TYPE_SRV:
            prio, weight, port = struct.unpack(">HHH", data[off:off + 6])
            target, _ = _read_name(data, off + 6)
            out.append((prio, weight, port, target))
        off += rdlen
    return out


# ---- Minecraft Server List Ping ----

def _varint(n: int) -> bytes:
    out = b""
    n &= 0xFFFFFFFF
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock) -> int:
    num = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed")
        num |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return num
    raise ValueError("varint too big")


def mc_status(host: str, port: int, timeout: float = 6.0) -> dict:
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    try:
        addr = host.encode()
        hs = (b"\x00" + _varint(-1) + _varint(len(addr)) + addr
              + struct.pack(">H", port) + _varint(1))
        s.sendall(_varint(len(hs)) + hs)
        s.sendall(_varint(1) + b"\x00")
        _read_varint(s)
        if _read_varint(s) != 0:
            raise ValueError("unexpected packet id")
        jlen = _read_varint(s)
        buf = b""
        while len(buf) < jlen:
            chunk = s.recv(jlen - len(buf))
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8", "replace"))
    finally:
        s.close()


def _motd_text(status: dict) -> str:
    d = status.get("description", "")
    if isinstance(d, dict):
        text = d.get("text", "")
        if not text and "extra" in d:
            text = "".join(e.get("text", "") if isinstance(e, dict) else str(e)
                           for e in d["extra"])
        return text or json.dumps(d, ensure_ascii=False)
    return str(d)


@dataclass
class ConnTestResult:
    fqdn: str
    resolver: str
    used_srv: bool
    endpoint: str          # 接続した host:port
    online: bool
    motd: str = ""
    version: str = ""
    players: str = ""
    error: str = ""


def test_server(fqdn: str, resolver: str = "8.8.8.8",
                service: str = "minecraft", default_port: int = 25565) -> ConnTestResult:
    """外部プレイヤー視点で fqdn へ接続を試み、届いたサーバーのMOTD等を返す。"""
    used_srv = False
    host, port = fqdn, default_port
    try:
        srv = dns_query(resolver, f"_{service}._tcp.{fqdn}", _TYPE_SRV)
        if srv:
            srv.sort(key=lambda r: (r[0], -r[1]))   # priority昇順, weight降順
            _, _, port, target = srv[0]
            host = target.rstrip(".") or fqdn
            used_srv = True
    except Exception:
        pass
    # ターゲットのA(指定リゾルバで)
    try:
        a = dns_query(resolver, host, _TYPE_A)
        ip = a[0] if a else host
    except Exception:
        ip = host
    try:
        st = mc_status(ip, port)
        return ConnTestResult(
            fqdn=fqdn, resolver=resolver, used_srv=used_srv,
            endpoint=f"{ip}:{port}", online=True,
            motd=_motd_text(st),
            version=st.get("version", {}).get("name", ""),
            players=f"{st.get('players', {}).get('online', '?')}/"
                    f"{st.get('players', {}).get('max', '?')}")
    except Exception as exc:
        return ConnTestResult(
            fqdn=fqdn, resolver=resolver, used_srv=used_srv,
            endpoint=f"{ip}:{port}", online=False,
            error=f"{type(exc).__name__}: {exc}")
