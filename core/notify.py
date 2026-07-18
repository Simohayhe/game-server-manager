"""Discord Webhook通知。

サーバーの起動/停止/クラッシュ/再起動/バックアップ完了/入退室/ポート開閉などを
Discord へ通知する。Webhookに向けて JSON を POST するだけ(外部ライブラリ不要)。

送信先は複数持てる(notify.json の destinations)。送信先ごとに「何を通知するか」を
個別に選べる(例: 管理用チャンネルには全部、みんなが見るチャンネルには入退室だけ)。
旧形式(単一 webhook_url + events)は読み込み時に1つの送信先へ自動移行する。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# 通知イベントの種類と既定ON/OFF
DEFAULT_EVENTS = {
    "server_up": True,     # サーバー起動
    "server_down": True,   # サーバー停止(意図的)
    "crash": True,         # 予期せぬ停止(クラッシュ)
    "restart": True,       # 再起動(予約含む)
    "backup": True,        # バックアップ完了
    "update": True,        # サーバー更新あり(ARK)
    "player_join": True,   # プレイヤー入室(誰がどのサーバーに入ったか)
    "player_leave": True,  # プレイヤー退室
    "dns_down": True,      # DNS障害(名前解決できない=外部参加の生命線)
    "dns_recover": True,   # DNS復旧
    "port": False,         # ポート開閉(既定OFF=うるさいので)
}
EVENT_LABELS = {
    "server_up": "サーバー起動", "server_down": "サーバー停止",
    "crash": "クラッシュ検知", "restart": "再起動",
    "backup": "バックアップ完了", "update": "更新あり",
    "player_join": "プレイヤー入室", "player_leave": "プレイヤー退室",
    "dns_down": "DNS障害", "dns_recover": "DNS復旧",
    "port": "ポート開閉",
}

# 通知をゲーム種別で絞れるようにする(Discordのチャンネルをゲームごとに分けている人向け)。
# 既定は全部ON。ゲームに紐付かない通知(ポート等)は game=None で送られ、この絞りを素通りする。
DEFAULT_GAMES = {"ark": True, "palworld": True, "minecraft": True}
GAME_LABELS = {"ark": "ARK", "palworld": "Palworld", "minecraft": "Minecraft"}


def _norm_events(raw: dict | None) -> dict:
    ev = dict(DEFAULT_EVENTS)
    ev.update({k: bool(v) for k, v in (raw or {}).items() if k in DEFAULT_EVENTS})
    return ev


def _norm_games(raw: dict | None) -> dict:
    g = dict(DEFAULT_GAMES)
    g.update({k: bool(v) for k, v in (raw or {}).items() if k in DEFAULT_GAMES})
    return g


@dataclass
class Destination:
    """1つの送信先(Discordチャンネル)。通知内容(events)とゲーム(games)を個別に持つ。"""
    name: str = "送信先"
    webhook_url: str = ""
    enabled: bool = True
    events: dict = field(default_factory=lambda: dict(DEFAULT_EVENTS))
    games: dict = field(default_factory=lambda: dict(DEFAULT_GAMES))

    def wants(self, event: str, game: str | None = None) -> bool:
        if not (self.enabled and self.webhook_url and self.events.get(event, False)):
            return False
        if game is None:                 # ゲームに紐付かない通知(ポート等)は素通り
            return True
        return bool(self.games.get(game, True))

    def to_dict(self) -> dict:
        return {"name": self.name, "webhook_url": self.webhook_url,
                "enabled": self.enabled, "events": dict(self.events),
                "games": dict(self.games)}

    @classmethod
    def from_dict(cls, d: dict) -> "Destination":
        return cls(name=str(d.get("name") or "送信先"),
                   webhook_url=str(d.get("webhook_url", "")),
                   enabled=bool(d.get("enabled", True)),
                   events=_norm_events(d.get("events")),
                   games=_norm_games(d.get("games")))


@dataclass
class NotifyConfig:
    enabled: bool = False                       # 全体のマスタースイッチ
    destinations: list = field(default_factory=list)

    def targets(self, event: str, game: str | None = None) -> list:
        """このイベント(かつゲーム)を受け取るべき送信先の一覧。マスターOFFなら空。"""
        if not self.enabled:
            return []
        return [d for d in self.destinations if d.wants(event, game)]

    def wants(self, event: str, game: str | None = None) -> bool:
        """後方互換: どこか1つでも受け取るなら True。"""
        return bool(self.targets(event, game))

    def to_dict(self) -> dict:
        return {"enabled": self.enabled,
                "destinations": [d.to_dict() for d in self.destinations]}


def load(path: str | Path) -> NotifyConfig:
    p = Path(path)
    if not p.exists():
        return NotifyConfig()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NotifyConfig()

    if isinstance(d.get("destinations"), list):        # 新形式
        dests = [Destination.from_dict(x) for x in d["destinations"] if isinstance(x, dict)]
    elif d.get("webhook_url"):                         # 旧形式 → 1件へ移行
        dests = [Destination(name="既定", webhook_url=str(d["webhook_url"]),
                             enabled=True, events=_norm_events(d.get("events")))]
    else:
        dests = []
    return NotifyConfig(enabled=bool(d.get("enabled", False)), destinations=dests)


def save(path: str | Path, cfg: NotifyConfig) -> None:
    Path(path).write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")


def config_from_dict(d: dict) -> NotifyConfig:
    """API/GUIから来た辞書を NotifyConfig にする(保存前の検証用)。"""
    dests = [Destination.from_dict(x) for x in (d.get("destinations") or [])
             if isinstance(x, dict)]
    return NotifyConfig(enabled=bool(d.get("enabled", False)), destinations=dests)


def send(webhook_url: str, text: str, username: str = "GameServerManager",
         timeout: float = 8) -> None:
    """Discord Webhookへメッセージを送る(例外は呼び出し側で扱う)。"""
    if not webhook_url:
        raise ValueError("Webhook URLが未設定です")
    payload = json.dumps({"content": text[:1900], "username": username}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "GSM/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status not in (200, 204):
            raise urllib.error.HTTPError(webhook_url, resp.status, "webhook失敗",
                                         resp.headers, None)
