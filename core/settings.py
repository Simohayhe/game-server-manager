"""config.yamlのアプリ内編集(コメントを保持したまま書き換える)。

GUIの設定タブから使う。編集後にload_configで検証し、壊れていたら元に戻す。
"""
from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 120


class SettingsError(Exception):
    pass


def read_raw(path: str | Path) -> dict:
    """config.yamlを生のまま(コメント付き構造で)読む。表示用。"""
    with open(path, encoding="utf-8") as f:
        return _yaml.load(f) or {}


def set_ark_display_name(path: str | Path, index: int, name: str) -> None:
    """ark_hosts[index].display_name を更新(コメント保持・検証・ロールバック)。"""
    from .config import load_config  # 循環import回避
    name = (name or "").strip()
    if not name:
        raise SettingsError("名前が空です")
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    with open(path, encoding="utf-8") as f:
        data = _yaml.load(f) or {}
    hosts = data.get("ark_hosts")
    if not hosts or index < 0 or index >= len(hosts):
        raise SettingsError(f"ark_hosts[{index}] が見つかりません")
    hosts[index]["display_name"] = name
    with open(path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    try:
        load_config(path)  # 検証
    except Exception as exc:
        path.write_text(original, encoding="utf-8")  # ロールバック
        raise SettingsError(f"設定の検証に失敗したため元に戻しました: {exc}") from exc


def update_config(path: str | Path, updates: dict) -> None:
    """指定セクションの値を更新して保存する(コメント保持・検証・ロールバック付き)。

    updates例: {"network": {"subnet": "...", "vm_range": "100-199"},
                "dns": {"ssh": {"password": "..."}}}
    """
    from .config import load_config  # 循環import回避

    path = Path(path)
    original = path.read_text(encoding="utf-8")

    with open(path, encoding="utf-8") as f:
        data = _yaml.load(f) or {}

    def merge(node, values) -> None:
        for key, value in values.items():
            if isinstance(value, dict):
                if key not in node or node[key] is None:
                    node[key] = {}
                merge(node[key], value)
            else:
                node[key] = value

    merge(data, updates)

    with open(path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    try:
        load_config(path)  # 検証
    except Exception as exc:
        path.write_text(original, encoding="utf-8")  # ロールバック
        raise SettingsError(f"設定の検証に失敗したため元に戻しました: {exc}") from exc
