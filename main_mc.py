"""Minecraft GUI のエントリポイント。

    python main_mc.py                  # VM上の全サーバー(MC+Palworld)
    python main_mc.py --game minecraft # マイクラだけに絞る

常駐サービス(main_service.py)が動いている前提。閉じても予約・バックアップは止まらない。
"""
from __future__ import annotations

import argparse
import sys

from gui.server_app import run


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM Minecraft GUI")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    ap.add_argument("--game", default="minecraft",
                    help="絞り込むゲーム種別(all で全部)")
    args = ap.parse_args()
    game = None if args.game == "all" else args.game
    run(args.base, game=game, title="GSM — Minecraft")
    return 0


if __name__ == "__main__":
    sys.exit(main())
