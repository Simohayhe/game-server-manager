"""GSM コンソール(CLI)の実行ファイル用エントリ(console exe としてビルドする)。

  gsm.exe status
  gsm.exe start minecraft4
  gsm.exe --url http://<host>:8770 --token <token> status

main_app.py --cli と同じ中身だが、こちらは軽量なコンソールexeにするための入口。
GUI(customtkinter)や paramiko を巻き込まないので小さく・速い。
"""
from __future__ import annotations

import argparse
import os
import sys

from gui.cli import run_cli

DEFAULT_URL = "http://127.0.0.1:8770"


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--url", default=os.environ.get("GSM_URL", DEFAULT_URL))
    ap.add_argument("--token", default="")
    args, rest = ap.parse_known_args()
    if args.token:
        os.environ["GSM_TOKEN"] = args.token      # Client がヘッダに載せる
    return run_cli(args.url, rest)


if __name__ == "__main__":
    sys.exit(main())
