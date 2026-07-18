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

# steamcmd.exe はコンソールアプリなので、そのまま起動すると別ウィンドウ(cmd窓)が出る。
# 出力は PIPE でGUIのタスクログに流しているので、ウィンドウは抑止する(Windowsのみ)。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
import time
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
        timeout=timeout, errors="replace", creationflags=_NO_WINDOW)
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


def _update_once(exe: Path, install_dir: str | Path, progress, timeout: int,
                 app_id: int) -> str:
    """SteamCMDを1回実行する。失敗したら RuntimeError。"""
    args = [str(exe), "+force_install_dir", str(install_dir),
            "+login", "anonymous", "+app_update", str(app_id), "validate", "+quit"]
    proc = subprocess.Popen(
        args, cwd=str(exe.parent), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace",
        creationflags=_NO_WINDOW)
    last = ""
    for line in proc.stdout:                      # 逐次進捗
        line = line.rstrip()
        if line:
            last = line
            progress(line)
    proc.wait(timeout=timeout)
    if "Success" not in last and proc.returncode not in (0,):
        raise RuntimeError(f"終了コード {proc.returncode}: {last}")
    return installed_buildid(install_dir) or "?"


def _reset_appmanifest(install_dir: str | Path, app_id: int, progress) -> bool:
    """appmanifest_<id>.acf を退避して「新規インストール扱い」にする。

    既存インストールの acf は導入済みビルドの古いマニフェストIDを指しており、
    SteamCMDはそれを使って差分更新しようとする。Steamが古いマニフェストの
    配信を止めると 'Failed to get manifest request code, Access Denied' で
    永久に更新できなくなる(実機で確認: 30回連続で同じ拒否)。
    acfを退避すると最新マニフェストを取りに行くので復旧する。ファイル自体は
    残っているので validate による差分照合が効き、全再DLにはならない。
    """
    acf = Path(install_dir) / "steamapps" / f"appmanifest_{app_id}.acf"
    if not acf.exists():
        return False
    bak = acf.with_suffix(acf.suffix + f".bak_{time.strftime('%Y%m%d_%H%M%S')}")
    try:
        acf.rename(bak)
    except OSError as exc:
        progress(f"appmanifestの退避に失敗: {exc}")
        return False
    progress(f"appmanifestを退避({bak.name}) → 最新マニフェストで取り直します")
    return True


def update(steamcmd_path: str | Path, install_dir: str | Path,
           progress=lambda t: None, timeout: int = 3600,
           app_id: int = APP_ID, retries: int = 3, retry_wait: int = 60) -> str:
    """SteamCMDで更新/インストールする(対象停止済み前提)。標準出力を progress へ流す。

    失敗したら retry_wait 秒あけて retries 回まで再試行する。
    さらに「古いマニフェストが配信終了していて差分更新できない」ケース
    (Access Denied で何度やっても失敗)に備え、最後の1回は appmanifest を
    退避して新規扱いで取り直す。
    """
    exe = Path(steamcmd_path)
    if not exe.exists():
        raise FileNotFoundError(f"steamcmd が見つかりません: {exe}")
    last_err = None
    total = max(1, retries)
    for attempt in range(1, total + 1):
        if attempt > 1:
            progress(f"⏳ {retry_wait}秒待って再試行します({attempt}/{total})…")
            time.sleep(retry_wait)
        # 2回目以降は acf を退避して「新規インストール扱い」で取り直す。
        # 実機ではこれが本命の復旧策(古いマニフェスト配信終了による Access Denied)。
        # 1回目は通常の差分更新を試す(そちらで通るなら軽いので)。
        if attempt >= 2:
            _reset_appmanifest(install_dir, app_id, progress)
        progress(f"SteamCMDで更新を開始…({attempt}/{total}回目)")
        try:
            return _update_once(exe, install_dir, progress, timeout, app_id)
        except RuntimeError as exc:
            last_err = exc
            progress(f"⚠ 失敗({attempt}/{total}): {exc}")
    raise RuntimeError(f"{total}回試行しても更新できませんでした → {last_err}")
