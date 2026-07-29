"""接続専用GUIの実行ファイル用エントリ(windowed exe としてビルドする)。

このexeは「常駐サービスを起動しない・セットアップもしない」。
既にどこかで動いているGSMサービス(このPC or 別PCの 127.0.0.1:8770 相当)に
GUIで繋ぐだけ。別PC運用や、サービスは常駐タスク任せで画面だけ開きたい時に使う。

  GSM-Connect.exe                                  → ローカル(127.0.0.1:8770)
  GSM-Connect.exe http://192.168.11.5:8770 --token <token>   → 別PC
接続先は環境変数 GSM_URL / GSM_TOKEN でも指定できる。
"""
from __future__ import annotations

import argparse
import os
import sys

from gui.app_ctk import DEFAULT_BASE, run


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("url", nargs="?",
                    default=os.environ.get("GSM_URL", DEFAULT_BASE))
    ap.add_argument("--token", default="")
    args, _ = ap.parse_known_args()
    if args.token:
        os.environ["GSM_TOKEN"] = args.token      # Client がヘッダに載せる
    run(args.url)                                  # サービスは起こさない
    return 0


if __name__ == "__main__":
    sys.exit(main())
