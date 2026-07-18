"""Source RCONプロトコルのクライアント実装。

MinecraftもARK(Source RCON)も同じプロトコルで通信できるため、
外部ライブラリなしのソケット実装で共通化する。
"""
from __future__ import annotations

import socket
import struct

_TYPE_AUTH = 3
_TYPE_AUTH_RESPONSE = 2
_TYPE_EXEC = 2
_TYPE_RESPONSE = 0


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


class RconClient:
    # 応答が途切れたと判断するまでの猶予(lenient時のみ)。
    _SETTLE = 0.4

    def __init__(self, host: str, port: int, password: str, timeout: float = 5):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 1

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        req_id = self._send(_TYPE_AUTH, self.password)
        # Minecraftは認証成功時に空のRESPONSEを先に返すことがあるので、
        # AUTH_RESPONSEが来るまで読み進める
        while True:
            pkt_id, pkt_type, _ = self._recv()
            if pkt_type == _TYPE_AUTH_RESPONSE:
                if pkt_id == -1:
                    raise RconAuthError("RCON認証に失敗しました(パスワードを確認してください)")
                if pkt_id != req_id:
                    raise RconError("RCON認証応答のIDが一致しません")
                return

    def command(self, cmd: str, strict: bool = True) -> str:
        if self._sock is None:
            raise RconError("接続されていません")
        req_id = self._send(_TYPE_EXEC, cmd)
        # strict=False = Palworld。長さフィールドも信用できないので寛容に読む。
        pkt_id, _, body = self._recv(lenient=not strict)
        # PalworldのRCONは応答に要求IDを返さない(仕様非準拠)。strict=Falseで許容する。
        if strict and pkt_id != req_id:
            raise RconError("RCON応答のIDが一致しません")
        return body

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _send(self, pkt_type: int, body: str) -> int:
        pkt_id = self._next_id
        self._next_id += 1
        payload = struct.pack("<ii", pkt_id, pkt_type) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return pkt_id

    def _recv(self, lenient: bool = False) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._read_exact(4))
        payload = self._read_exact(length, lenient=lenient)
        pkt_id, pkt_type = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", "replace")
        return pkt_id, pkt_type, body

    def _read_exact(self, n: int, lenient: bool = False) -> bytes:
        """n バイト読む。lenient=True なら届いた分で打ち切ることを許す。

        PalworldのRCONは応答に非ASCII(日本語のプレイヤー名など)が含まれると、
        長さフィールドを実際の送信バイト数より大きく申告してくる(実測: 非ASCII1文字につき
        +2バイト。「サイオン」4文字なら 申告97 / 実際89)。厳密に n バイト待つと、
        永遠に来ない差分を待ち続けてタイムアウトする。届いた分が本文としては完全なので、
        データが途切れたらそこで打ち切る。
        """
        chunks = b""
        try:
            while len(chunks) < n:
                try:
                    chunk = self._sock.recv(n - len(chunks))
                except socket.timeout:
                    if lenient and chunks:
                        return chunks
                    raise
                if not chunk:
                    if lenient and chunks:
                        return chunks
                    raise RconError("サーバーが接続を切断しました")
                chunks += chunk
                if lenient:
                    self._sock.settimeout(self._SETTLE)   # 続きが来ないなら即打ち切る
        finally:
            if lenient:
                self._sock.settimeout(self.timeout)
        return chunks
