"""Discord Webhook通知。

サーバーの起動/停止/クラッシュ/再起動/バックアップ完了/ポート開閉などを Discord へ通知する。
Webhookに向けて JSON を POST するだけ(外部ライブラリ不要)。設定は notify.json に永続化。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 通知イベントの種類と既定ON/OFF
DEFAULT_EVENTS = {
    "server_up": True,     # サーバー起動
    "server_down": True,   # サーバー停止(意図的)
    "crash": True,         # 予期せぬ停止(クラッシュ)
    "restart": True,       # 再起動(予約含む)
    "backup": True,        # バックアップ完了
    "update": True,        # サーバー更新あり(ARK)
    "port": False,         # ポート開閉(既定OFF=うるさいので)
}
EVENT_LABELS = {
    "server_up": "サーバー起動", "server_down": "サーバー停止",
    "crash": "クラッシュ検知", "restart": "再起動",
    "backup": "バックアップ完了", "update": "更新あり", "port": "ポート開閉",
}


@dataclass
class NotifyConfig:
    enabled: bool = False
    webhook_url: str = ""
    events: dict = field(default_factory=lambda: dict(DEFAULT_EVENTS))

    def wants(self, event: str) -> bool:
        return self.enabled and bool(self.webhook_url) and self.events.get(event, False)


def load(path: str | Path) -> NotifyConfig:
    p = Path(path)
    if not p.exists():
        return NotifyConfig()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NotifyConfig()
    ev = dict(DEFAULT_EVENTS)
    ev.update({k: bool(v) for k, v in (d.get("events") or {}).items() if k in DEFAULT_EVENTS})
    return NotifyConfig(enabled=bool(d.get("enabled", False)),
                        webhook_url=str(d.get("webhook_url", "")), events=ev)


def save(path: str | Path, cfg: NotifyConfig) -> None:
    Path(path).write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
                          encoding="utf-8")


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
            raise urllib.error.HTTPError(webhook_url, resp.status, "webhook失敗", resp.headers, None)
