"""Palworld GUI のエントリポイント。

    python main_pal.py

常駐サービス(main_service.py)が動いている前提。
※ Palworldは常時プレイヤーが居る本番なので、停止/再起動は確認ダイアログ必須。
"""
from __future__ import annotations

import argparse
import sys

from gui.server_app import run


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM Palworld GUI")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    args = ap.parse_args()
    run(args.base, game="palworld", title="GSM — Palworld")
    return 0


if __name__ == "__main__":
    sys.exit(main())
