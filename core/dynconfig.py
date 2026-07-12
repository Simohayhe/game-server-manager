"""ARK dynamic config(CustomDynamicConfigUrl)対応。

ARKは -UseDynamicConfig + CustomDynamicConfigUrl=http://... を設定すると、
指定URLの設定ファイルを「ワールド自動保存ごと」または RCON `ForceUpdateDynamicConfig` で
再取得し、一部の倍率/イベント設定を**サーバー再起動なし**で反映できる。

GSMはホスト上で動くので、ローカルHTTPサーバー(127.0.0.1)で dynamicconfig.ini を text/plain 配信し、
ARK側にそのURLを向ける。対象は倍率・イベント系のみ(ルール/構造/boolは従来どおり再起動が必要)。

制約(ARK仕様): URLは HTTP のみ(HTTPS不可)、内容は text/plain。ファイルに載せた行だけが上書きされ、
載せない設定は通常の設定のまま(=設定ごとにON/OFFできる)。
"""
from __future__ import annotations

import http.server
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

DYN_PORT_DEFAULT = 8712
DYN_FILENAME = "dynamicconfig.ini"

# dynamic configで無停止変更できる設定(倍率・イベント系)。(key, 種別, 日本語ラベル, 既定)
DYN_SETTINGS = [
    ("TamingSpeedMultiplier", "float", "テイム速度倍率", "1.0"),
    ("HarvestAmountMultiplier", "float", "採取量倍率", "1.0"),
    ("HarvestHealthMultiplier", "float", "資源の耐久倍率", "1.0"),
    ("XPMultiplier", "float", "経験値(XP)倍率", "1.0"),
    ("MatingIntervalMultiplier", "float", "交配クールダウン倍率(小=早い)", "1.0"),
    ("MatingSpeedMultiplier", "float", "発情速度倍率(大=速い)", "1.0"),
    ("EggHatchSpeedMultiplier", "float", "卵の孵化速度倍率(大=速い)", "1.0"),
    ("BabyMatureSpeedMultiplier", "float", "赤ちゃん成長速度倍率(大=速い)", "1.0"),
    ("BabyCuddleIntervalMultiplier", "float", "刷り込み間隔倍率(小=頻度↓)", "1.0"),
    ("BabyImprintAmountMultiplier", "float", "刷り込み量倍率", "1.0"),
    ("CropGrowthSpeedMultiplier", "float", "作物の成長速度倍率", "1.0"),
    ("HexagonRewardMultiplier", "float", "ヘキサゴン報酬倍率", "1.0"),
    ("MateBoostEffectMultiplier", "float", "つがいブースト効果倍率", "1.0"),
]
DYN_KEYS = {k for k, _t, _l, _d in DYN_SETTINGS}


@dataclass
class DynState:
    enabled: bool = False                       # マスターON/OFF
    port: int = DYN_PORT_DEFAULT
    values: dict = field(default_factory=dict)  # key -> value(載せる=上書きON)


def load_state(path: str | Path) -> DynState:
    p = Path(path)
    if not p.exists():
        return DynState()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DynState()
    vals = {k: str(v) for k, v in (d.get("values") or {}).items() if k in DYN_KEYS}
    return DynState(enabled=bool(d.get("enabled", False)),
                    port=int(d.get("port", DYN_PORT_DEFAULT)), values=vals)


def save_state(path: str | Path, state: DynState) -> None:
    Path(path).write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2),
                          encoding="utf-8")


def build_content(values: dict) -> str:
    """dynamicconfig.ini の中身。載せた行だけが上書きされる(flat key=value)。"""
    lines = [f"{k}={v}" for k, v in values.items() if k in DYN_KEYS]
    return "\r\n".join(lines) + ("\r\n" if lines else "")


def write_file(file_path: str | Path, values: dict) -> None:
    Path(file_path).write_text(build_content(values), encoding="utf-8")


def _make_handler(file_path: str):
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # パスに関わらずファイル内容を返す
            try:
                data = Path(file_path).read_bytes()
            except OSError:
                data = b""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def log_message(self, *_a):             # アクセスログは静音
            pass
    return _H


class DynConfigServer:
    """dynamicconfig.ini を 127.0.0.1 で text/plain 配信するローカルHTTPサーバー。"""

    def __init__(self, file_path: str | Path, port: int = DYN_PORT_DEFAULT):
        self.file_path = str(file_path)
        self.port = port
        self._httpd = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{DYN_FILENAME}"

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), _make_handler(self.file_path))
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        finally:
            self._httpd = None
            self._thread = None
