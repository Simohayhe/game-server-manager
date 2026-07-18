"""GSM 統合GUI(customtkinter版)のエントリポイント。

    python main_gsm.py

  ダッシュボード(状態カード + CPU/メモリ/人数グラフ)
  ゲームサーバー  🦖 ARK / 🐑 Palworld / 🟩 Minecraft   ← ライブログ付き
  システム       🖥 VM / ⏰ 予約 / 📋 タスク

常駐サービス(main_service.py)が動いている前提。この画面を閉じても
予約・バックアップ・動的設定配信は止まらない。
"""
from __future__ import annotations

import argparse
import sys

from gui.app_ctk import run


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM 統合GUI")
    ap.add_argument("--base", default="http://127.0.0.1:8770",
                    help="常駐サービスのAPI URL")
    args = ap.parse_args()
    run(args.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
