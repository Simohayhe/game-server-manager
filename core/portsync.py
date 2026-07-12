"""自動ポート開放(サーバー起動中だけポートを開ける)。

方針: 常時開放はしない。サーバーが起動していれば対応ポートを開け、停止したら閉じる。
UPnPで GSM が開けたマッピングだけ(description が gsm-auto で始まるもの)を開閉対象にし、
手動公開(_sv_publish の gsm-<name>)や他機器のマッピングには触らない。

- Minecraft(VM): TCP、外部ポート → VMのIP:ゲームポート
- ARK(ホスト): UDP、ゲームポート/クエリポート → ホストのLAN IP:同ポート

状態(有効/無効)は portsync.json に永続化する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

AUTO_DESC_PREFIX = "gsm-auto"


@dataclass
class PortSpec:
    label: str           # 識別用(サーバー名やマップ名+用途)
    ext_port: int        # 外部(WAN)ポート
    internal_ip: str     # 転送先IP(VM or ホスト)
    internal_port: int   # 転送先ポート
    proto: str           # "TCP" or "UDP"
    desired: bool        # 開けたい(=サーバー起動中)か

    @property
    def desc(self) -> str:
        return f"{AUTO_DESC_PREFIX}-{self.label}"


def load_enabled(path: str | Path) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("enabled", False))
    except (OSError, ValueError):
        return False


def save_enabled(path: str | Path, enabled: bool) -> None:
    Path(path).write_text(json.dumps({"enabled": bool(enabled)}), encoding="utf-8")


def is_ours(mapping: dict) -> bool:
    return str(mapping.get("description", "")).startswith(AUTO_DESC_PREFIX)
