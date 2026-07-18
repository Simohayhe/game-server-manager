"""ARK専用GUIのエントリポイント。

    python main_ark.py [--base http://127.0.0.1:8770]

常駐サービス(main_service.py)が動いている前提。このGUIを閉じても、
予約・バックアップ・動的設定配信はサービス側で動き続ける。
"""
from __future__ import annotations

import argparse
import sys

from gui.ark_app import run


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM ARK GUI")
    ap.add_argument("--base", default="http://127.0.0.1:8770",
                    help="常駐サービスのAPI URL")
    args = ap.parse_args()
    run(args.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
