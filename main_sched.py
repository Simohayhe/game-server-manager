"""予約(スケジューラ)GUI のエントリポイント。

    python main_sched.py

予約の実体は常駐サービスが持つので、この画面を閉じても予約は動き続ける。
"""
from __future__ import annotations

import argparse
import sys

from gui.sched_app import run


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM 予約GUI")
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    args = ap.parse_args()
    run(args.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
