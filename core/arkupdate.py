"""ARK(ASA)専用サーバーの更新チェック/更新(SteamCMD)。

ASA Dedicated Server は Steam App ID 2430930。SteamCMDで更新する。
  - 導入済みビルド: <install_dir>/steamapps/appmanifest_2430930.acf の buildid
  - 最新ビルド    : steamcmd +app_info_print 2430930 の public ブランチ buildid
  - 更新          : steamcmd +force_install_dir <install_dir> +app_update 2430930 validate
更新中は対象exeが使われていないこと(全マップ停止)が前提。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

APP_ID = 2430930          # ARK: Survival Ascended Dedicated Server(既定)
PAL_APP_ID = 2394010      # Palworld Dedicated Server


def installed_buildid(install_dir: str | Path, app_id: int = APP_ID) -> str | None:
    acf = Path(install_dir) / "steamapps" / f"appmanifest_{app_id}.acf"
    if not acf.exists():
        return None
    m = re.search(r'"buildid"\s*"(\d+)"',
                  acf.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def latest_buildid(steamcmd_path: str | Path, app_id: int = APP_ID,
                   timeout: int = 240) -> str | None:
    """SteamCMDで public ブランチの最新 buildid を取得する。"""
    exe = Path(steamcmd_path)
    if not exe.exists():
        raise FileNotFoundError(f"steamcmd が見つかりません: {exe}")
    proc = subprocess.run(
        [str(exe), "+login", "anonymous", "+app_info_update", "1",
         "+app_info_print", str(app_id), "+quit"],
        cwd=str(exe.parent), capture_output=True, text=True,
        timeout=timeout, errors="replace")
    out = proc.stdout or ""
    # "public" ブランチ直下の buildid(public_test_realm 等は誤マッチしない)
    m = re.search(r'"public"\s*\{\s*"buildid"\s*"(\d+)"', out)
    return m.group(1) if m else None


def check(steamcmd_path: str | Path, install_dir: str | Path,
          app_id: int = APP_ID) -> dict:
    inst = installed_buildid(install_dir, app_id)
    latest = latest_buildid(steamcmd_path, app_id)
    return {
        "installed": inst,
        "latest": latest,
        "update_available": bool(inst and latest and inst != latest),
    }


def update(steamcmd_path: str | Path, install_dir: str | Path,
           progress=lambda t: None, timeout: int = 3600,
           app_id: int = APP_ID) -> str:
    """SteamCMDで更新/インストールする(対象停止済み前提)。標準出力を progress へ流す。"""
    exe = Path(steamcmd_path)
    if not exe.exists():
        raise FileNotFoundError(f"steamcmd が見つかりません: {exe}")
    args = [str(exe), "+force_install_dir", str(install_dir),
            "+login", "anonymous", "+app_update", str(app_id), "validate", "+quit"]
    progress("SteamCMDで更新を開始…(数分かかる場合があります)")
    proc = subprocess.Popen(
        args, cwd=str(exe.parent), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
    last = ""
    for line in proc.stdout:                      # 逐次進捗
        line = line.rstrip()
        if line:
            last = line
            progress(line)
    proc.wait(timeout=timeout)
    if "Success" not in last and proc.returncode not in (0,):
        raise RuntimeError(f"更新に失敗した可能性(終了コード {proc.returncode}): {last}")
    return installed_buildid(install_dir) or "?"
