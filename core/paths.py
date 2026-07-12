"""実行形態(通常 / PyInstaller exe)に応じたパス解決。

- app_dir()   : ユーザーデータ(config.yaml・各種.json・マーカー)を置く場所。
                exe化時は「exeの隣」、通常はプロジェクトルート。
- bundle_dir(): 同梱の読み取り専用データ(provisioners/ 等)の場所。
                exe化時は展開先(_MEIPASS)、通常はプロジェクトルート。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_dir() -> Path:
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]
