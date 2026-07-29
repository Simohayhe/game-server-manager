"""接続専用GUIの実行ファイル用エントリ(windowed exe としてビルドする)。

このexeは「常駐サービスを起動しない・セットアップもしない」。
既にどこかで動いているGSMサービス(このPC or 別PCの 127.0.0.1:8770 相当)に
GUIで繋ぐだけ。別PC運用や、サービスは常駐タスク任せで画面だけ開きたい時に使う。

  GSM-Connect.exe                                  → 接続画面(URL/パスワード入力)を表示
  GSM-Connect.exe http://192.168.11.5:8770 --password <pw>   → 直接接続(画面を出さない)
接続先は環境変数 GSM_URL / GSM_PASSWORD(=GSM_TOKEN)でも指定できる。
"""
from __future__ import annotations

import argparse
import os
import sys

from gui.app_ctk import run_connect


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("url", nargs="?", default=os.environ.get("GSM_URL"))
    ap.add_argument("--password", "--token", dest="password",
                    default=os.environ.get("GSM_PASSWORD")
                    or os.environ.get("GSM_TOKEN", ""))
    args, _ = ap.parse_known_args()
    # URL未指定なら接続画面を出す。指定時はそのまま繋ぐ。
    run_connect(args.url or None, args.password or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
