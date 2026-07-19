"""実行形態(通常 / PyInstaller exe)に応じたパス解決。

- app_dir()   : ユーザーデータ(config.yaml・各種.json・マーカー)を置く場所。
                exe化時は %LOCALAPPDATA%\\GameServerManager\\(exeの場所に依存しない安定した場所)。
                通常はプロジェクトルート。
- bundle_dir(): 同梱の読み取り専用データ(provisioners/ 等)の場所。
                exe化時は展開先(_MEIPASS)、通常はプロジェクトルート。

exe版のデータを「exeの隣」ではなく %LOCALAPPDATA% に置くのは、アップデートで
exeを入れ替えても設定/状態がそのまま残り、再セットアップ不要にするため。
旧バージョン(exe隣にデータを置く版)からは、初回に自動で引っ越す(_migrate_once)。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_APP_NAME = "GameServerManager"
_migrated = False


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def app_dir() -> Path:
    if not _frozen():
        return Path(__file__).resolve().parents[1]
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / _APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    _migrate_once(base)
    return base


def bundle_dir() -> Path:
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", _exe_dir()))
    return Path(__file__).resolve().parents[1]


def _migrate_once(dst: Path) -> None:
    """旧版(exe隣にデータを置く版)の設定/状態を、一度だけ dst へ引っ越す。

    - dst に config.yaml が既にあれば何もしない(移行済み or 新データあり)。
    - exe隣に config.yaml が無ければ何もしない(純粋な新規インストール)。
    - 対象は yaml/json と初回マーカー(状態系のみ。大きい content は移さない)。
    失敗しても起動は止めない(最悪セットアップからやり直せる)。
    """
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        src = _exe_dir()
        if src == dst or (dst / "config.yaml").exists():
            return
        if not (src / "config.yaml").exists():
            return
        for p in src.iterdir():
            if not p.is_file():
                continue
            if p.suffix in (".yaml", ".json") or p.name == ".gsm_firstrun_done":
                tgt = dst / p.name
                if not tgt.exists():
                    shutil.copy2(p, tgt)
    except Exception:                                          # noqa: BLE001
        pass
