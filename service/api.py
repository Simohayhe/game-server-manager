"""ローカルHTTP JSON API。GUI(別exe)はこれ経由で常駐サービスと話す。

方針:
  - 依存を増やさない: 標準ライブラリの ThreadingHTTPServer(動的設定配信と同じ方式)。
    将来Web/スマホ対応でFastAPIに載せ替えたくなってもルーティングを移すだけでよい
    (処理本体は service/ と core/ にあり、UIにもHTTPにも依存していない)。
  - 127.0.0.1 のみ待受(外部には開かない)。
  - 参照は StateCache から即返す。操作はジョブキューに投げてタスクIDを返すので
    HTTPが固まらない(進捗は /api/tasks で追える)。
"""
from __future__ import annotations

import http.server
import json
import re
import threading
from urllib.parse import urlparse

API_PORT_DEFAULT = 8770


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Router:
    """(メソッド, パス正規表現) -> ハンドラ の単純なルータ。"""

    def __init__(self):
        self._routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        self._routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str):
        for m, pat, h in self._routes:
            mo = pat.match(path)
            if mo and m == method.upper():
                return h, mo.groupdict()
        return None, None


def _handler_factory(router: Router):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError as exc:
                raise ApiError(400, f"JSONとして読めません: {exc}") from exc

        def _dispatch(self, method: str) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            handler, params = router.match(method, path)
            if handler is None:
                self._send(404, {"error": f"no route: {method} {path}"})
                return
            try:
                body = self._body() if method in ("POST", "PUT") else {}
                result = handler(params=params, body=body, query=urlparse(self.path).query)
                self._send(200, result if result is not None else {"ok": True})
            except ApiError as exc:
                self._send(exc.status, {"error": exc.message})
            except Exception as exc:                    # 予期しない例外も500で返す
                import traceback
                traceback.print_exc()
                self._send(500, {"error": str(exc)})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *a):                      # アクセスログは出さない
            pass
    return _H


class ApiServer:
    """常駐サービスに登録して使う(start/stop を持つ)。"""

    def __init__(self, router: Router, port: int = API_PORT_DEFAULT,
                 host: str = "127.0.0.1"):
        self.router = router
        self.host = host
        self.port = port
        self._srv: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._srv is not None

    def start(self) -> None:
        if self._srv is not None:
            return
        self._srv = http.server.ThreadingHTTPServer(
            (self.host, self.port), _handler_factory(self.router))
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        name="gsm-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
