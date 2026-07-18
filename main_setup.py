"""GSM セットアップ + 起動ランチャー(新アーキ用)。

    GSM-Setup.exe をダブルクリック

流れ:
  1. 動作環境チェック(Hyper-V等)を表示
  2. config.yaml が無ければ入力ウィザードで作成(YAML手編集不要)
  3. 常駐サービス(GSM-Service.exe)と GUI(GSM.exe)を起動

exe化していない時(開発中)は main_service.py / main_gsm.py を起動する。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import compat
from core.paths import app_dir
from gui import firstrun, setupwizard

BASE = app_dir()
CONFIG = BASE / "config.yaml"
MARKER = BASE / ".gsm_firstrun_done"


def _launch() -> None:
    """サービスとGUIを起動する(exeなら兄弟exe、開発中はpython)。"""
    if getattr(sys, "frozen", False):
        svc, gui = BASE / "GSM-Service.exe", BASE / "GSM.exe"
        if svc.exists():
            subprocess.Popen([str(svc)], cwd=str(BASE))
        if gui.exists():
            subprocess.Popen([str(gui)], cwd=str(BASE))
    else:
        py = sys.executable
        subprocess.Popen([py, str(BASE / "main_service.py")], cwd=str(BASE))
        subprocess.Popen([py, str(BASE / "main_gsm.py")], cwd=str(BASE))


def main() -> int:
    result = compat.check()
    if not MARKER.exists() or not result.get("suitable"):
        if firstrun.show(result) != "proceed":
            return 0
        try:
            MARKER.write_text("done", encoding="utf-8")
        except OSError:
            pass

    if not CONFIG.exists():
        if not setupwizard.run(CONFIG):     # キャンセルされたら起動しない
            return 0

    _launch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
