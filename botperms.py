"""Discordボットの操作権限ストア(誰がどのサーバーを操作できるか)。

`/permission add/remove/list` から更新される。JSON1ファイルに保存し、標準ライブラリのみ。

構造(permissions.json):
    {"users": {"<discord_user_id>": ["all" | "<game>" | "<server_key>", ...]}}

スコープ(権限)の意味:
    all           … 全サーバー
    <game>        … そのゲーム全体(minecraft / ark / palworld)
    <server_key>  … 個別サーバー(minecraft2 / ark-the-island 等)

「管理者」ロール保持者はこのストアと無関係に常に全操作可(discordbot側で判定)。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

PERMS_PATH = Path(__file__).resolve().parent / "permissions.json"
_lock = threading.Lock()


def load() -> dict:
    try:
        data = json.loads(PERMS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    return data


def save(data: dict) -> None:
    tmp = PERMS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PERMS_PATH)


def grants(user_id: int | str) -> set[str]:
    """そのユーザーに付与されているスコープ集合。"""
    return set(load()["users"].get(str(user_id), []))


def add(user_id: int | str, scope: str) -> bool:
    """スコープを付与。新規追加なら True、既に持っていれば False。"""
    with _lock:
        data = load()
        cur = data["users"].setdefault(str(user_id), [])
        if scope in cur:
            return False
        cur.append(scope)
        save(data)
        return True


def remove(user_id: int | str, scope: str) -> bool:
    """スコープを剥奪。実際に削除したら True、持っていなければ False。"""
    with _lock:
        data = load()
        uid = str(user_id)
        cur = data["users"].get(uid, [])
        if scope not in cur:
            return False
        cur.remove(scope)
        if cur:
            data["users"][uid] = cur
        else:
            data["users"].pop(uid, None)   # 空になったらキーごと消す
        save(data)
        return True


def allows(user_grants: set[str], target_scopes: set[str]) -> bool:
    """ユーザーの保有スコープが対象サーバーのスコープに1つでも当たれば True。"""
    return bool(user_grants & target_scopes)
