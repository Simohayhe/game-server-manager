"""サーバー状態のキャッシュ。

is_running() は PowerShell、players() は RCON で、どちらも数百ms〜秒かかる。
APIリクエストのたびに実行すると画面が固まるので、監視スレッド(monitor)が定期的に
更新した結果をここに置き、APIはキャッシュを返すだけにする。
(旧GUIも同じ考えで20〜30秒ごとに更新していた。その仕組みを常駐側へ移す)
"""
from __future__ import annotations

import threading
import time


class StateCache:
    """スレッドセーフな状態キャッシュ。監視が書き、APIとGUIが読む。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._ark: dict[int, dict] = {}        # index -> 状態
        self._servers: dict[str, dict] = {}    # name  -> 状態
        self._meta: dict = {}                  # 更新ビルドID・ホスト情報など
        self._updated: float = 0.0

    # ---- 書き込み(監視スレッドから) ----
    def set_ark(self, index: int, **fields) -> None:
        with self._lock:
            cur = self._ark.setdefault(index, {})
            cur.update(fields)
            cur["updated"] = time.time()
            self._updated = time.time()

    def set_server(self, name: str, **fields) -> None:
        with self._lock:
            cur = self._servers.setdefault(name, {})
            cur.update(fields)
            cur["updated"] = time.time()
            self._updated = time.time()

    def set_meta(self, **fields) -> None:
        with self._lock:
            self._meta.update(fields)
            self._updated = time.time()

    # ---- 読み出し(API/GUIから) ----
    def ark(self) -> list[dict]:
        with self._lock:
            return [dict(v, index=k) for k, v in sorted(self._ark.items())]

    def ark_one(self, index: int) -> dict | None:
        with self._lock:
            v = self._ark.get(index)
            return dict(v, index=index) if v else None

    def servers(self) -> list[dict]:
        with self._lock:
            return [dict(v, name=k) for k, v in sorted(self._servers.items())]

    def server_one(self, name: str) -> dict | None:
        with self._lock:
            v = self._servers.get(name)
            return dict(v, name=name) if v else None

    def meta(self) -> dict:
        with self._lock:
            return dict(self._meta)

    def snapshot(self) -> dict:
        return {
            "ark": self.ark(),
            "servers": self.servers(),
            "meta": self.meta(),
            "updated": self._updated,
        }

    def age(self) -> float:
        """最終更新からの経過秒。GUIが「情報が古い」と分かるように返す。"""
        return time.time() - self._updated if self._updated else -1.0
