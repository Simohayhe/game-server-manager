"""GSM 統合エントリ(1つのexeでセットアップ・常駐サービス・GUIを兼ねる)。

  GameServerManager.exe をダブルクリック

モード(引数):
  (なし)     初回セットアップ → サービスをバックグラウンド起動 → GUI表示
  --service  常駐サービスとして動く(自分自身をこのモードで起動する)

1ファイルだが、サービスは「自分自身を --service で別プロセス起動」するので、
GUIを閉じてもサービスは動き続ける(予約・監視・バックアップが止まらない)。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from core.paths import app_dir

BASE = app_dir()
CONFIG = BASE / "config.yaml"
MARKER = BASE / ".gsm_firstrun_done"
PORT = 8770
API = f"http://127.0.0.1:{PORT}"


def _service_alive() -> bool:
    try:
        urllib.request.urlopen(API + "/api/health", timeout=1.5)
        return True
    except Exception:
        return False


def _spawn_service() -> None:
    """自分自身を --service モードで、コンソール無し・独立プロセスとして起動する。"""
    flags = 0
    if os.name == "nt":                       # DETACHED_PROCESS | CREATE_NO_WINDOW
        flags = 0x00000008 | 0x08000000
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--service"]
    else:
        cmd = [sys.executable, str(BASE / "main_app.py"), "--service"]
    subprocess.Popen(cmd, cwd=str(BASE), creationflags=flags, close_fds=True)


def _kill_service() -> None:
    """稼働中の裏方サービス(--service プロセス)だけを止める。

    GUI本体は --service を付けずに起動しているので巻き込まない。
    """
    if os.name != "nt":
        subprocess.run(["pkill", "-f", "main_app.py --service"], check=False)
        return
    ps = ("Get-CimInstance Win32_Process | Where-Object { "
          "$_.CommandLine -match '--service' -and ($_.Name -eq 'python.exe' -or "
          "$_.Name -eq 'pythonw.exe' -or $_.Name -eq 'GameServerManager.exe') } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   creationflags=0x08000000, check=False)


def restart_service(timeout: float = 25.0) -> bool:
    """裏方サービスを止めて起動し直す(コード更新の反映など)。復帰でTrue。

    ゲームサーバー本体(ARK/MC/Palworld)には影響しない。GUIから呼ぶ想定。
    """
    _kill_service()
    for _ in range(20):                       # ポート(8770)が空くのを待つ
        if not _service_alive():
            break
        time.sleep(0.3)
    _spawn_service()
    end = time.time() + timeout
    while time.time() < end:
        if _service_alive():
            return True
        time.sleep(0.5)
    return False


def _run_service() -> int:
    from main_service import build_service
    try:
        svc = build_service(PORT)
    except Exception as exc:
        print(f"サービス起動に失敗: {exc}")
        return 1
    svc.run_forever()
    return 0


def _run_gui() -> int:
    from gui.app_ctk import run
    run(API)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GSM")
    ap.add_argument("--service", action="store_true",
                    help="常駐サービスとして動く(内部用)")
    args = ap.parse_args()

    if args.service:
        return _run_service()

    # ---- 通常起動: セットアップ → サービス → GUI ----
    from core import compat
    from gui import firstrun, setupwizard
    result = compat.check()
    if not MARKER.exists() or not result.get("suitable"):
        if firstrun.show(result) != "proceed":
            return 0
        try:
            MARKER.write_text("done", encoding="utf-8")
        except OSError:
            pass

    if not CONFIG.exists():
        if not setupwizard.run(CONFIG):       # キャンセル → 起動しない
            return 0

    if not _service_alive():                  # まだ動いていなければ常駐開始
        _spawn_service()
        for _ in range(30):                   # 立ち上がりを最大15秒待つ
            if _service_alive():
                break
            time.sleep(0.5)

    return _run_gui()


if __name__ == "__main__":
    sys.exit(main())
