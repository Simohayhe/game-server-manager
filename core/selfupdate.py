"""GitHub Release から最新の GameServerManager.exe をDLして自分自身を入れ替える。

exe(PyInstaller frozen)で動いている時だけ有効。流れ:
  1. latest_exe(repo) で最新リリースの exe 資産URLを得る
  2. download() で一時ファイルにDL
  3. apply_and_restart() がヘルパー(PowerShell)を起動 → 本体は終了
     → ヘルパーが GameServerManager.exe を落とす→新exeに差し替え→再起動

ユーザーデータは %LOCALAPPDATA%(core.paths)にあるので、exe を入れ替えても
設定・状態はそのまま(=再セットアップ不要)。標準ライブラリ + PowerShell のみ。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from core.updatecheck import _ver_tuple

_API = "https://api.github.com/repos/{repo}/releases?per_page=100"
ASSET_NAME = "GameServerManager.exe"
INSTALLER_NAME = "GameServerManager-Setup.exe"


def is_supported() -> bool:
    """frozen(exe)で動いている時だけ自己更新できる(開発時のsourceは不可)。"""
    return bool(getattr(sys, "frozen", False))


def _newest_release(repo: str, timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        _API.format(repo=repo),
        headers={"User-Agent": "game-server-manager",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        releases = json.load(r)
    cands = [x for x in releases
             if not x.get("draft") and not x.get("prerelease")]
    if not cands:
        return None
    return max(cands, key=lambda x: _ver_tuple(
        x.get("tag_name") or x.get("name") or ""))


def _asset_url(rel: dict, name: str) -> str | None:
    for a in rel.get("assets", []):
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None


def latest_exe(repo: str, timeout: float = 8.0) -> tuple[str | None, str | None]:
    """(tag, exe_url) を返す。exe資産が無ければ (tag, None)、失敗は (None, None)。"""
    try:
        rel = _newest_release(repo, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return (None, None)
    if not rel:
        return (None, None)
    tag = rel.get("tag_name") or rel.get("name")
    return (tag, _asset_url(rel, ASSET_NAME))


def latest_installer(repo: str, timeout: float = 8.0) -> tuple[str | None, str | None]:
    """(tag, installer_url) を返す。Setup.exe 資産があればその URL。
    無ければ (tag, None)、リリース未作成/失敗は (None, None)。"""
    try:
        rel = _newest_release(repo, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return (None, None)
    if not rel:
        return (None, None)
    tag = rel.get("tag_name") or rel.get("name")
    return (tag, _asset_url(rel, INSTALLER_NAME))


def download(url: str, dest: Path, progress=None, timeout: float = 120.0) -> Path:
    """url を dest にストリームDL。progress(got, total) を随時呼ぶ(totalが分かる時)。"""
    dest = Path(dest)
    req = urllib.request.Request(url, headers={"User-Agent": "game-server-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got, total)
    return dest


def run_installer(setup_exe: Path) -> None:
    """DL済みの Setup.exe を起動して更新させる。呼び出し後、本体は速やかに終了すること。

    Setup.exe(Inno Setup)は管理者マニフェスト付き=起動時にUAC昇格し、稼働中のGSMを停止して
    Program Files 等へ上書きインストール、完了後にGSMを再起動する。Program Files 方式でも
    昇格の面倒を Setup.exe に丸投げできるので、exe直接入替より確実。
    /SILENT で進捗バーのみ表示(ウィザードのクリック不要)。
    """
    setup_exe = Path(setup_exe).resolve()
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        [str(setup_exe), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        creationflags=DETACHED_PROCESS, close_fds=True)


def _ps_quote(p: Path) -> str:
    """PowerShell の単一引用符リテラル用にパスをエスケープ。"""
    return "'" + str(p).replace("'", "''") + "'"


def apply_and_restart(new_exe: Path, target_exe: Path | None = None) -> None:
    """DL済みの新exeで自分を置き換えて再起動する。呼び出し後、本体は速やかに終了すること。

    ヘルパー(PowerShell)が: 少し待つ → GameServerManager.exe を全て停止 →
    新exeを本来の場所へ移動(上書き) → 新exeを起動 → 自分(ps1)を削除。
    PowerShell を使うのは日本語を含むパスでも確実に扱えるため(batはOEM依存で不安定)。
    """
    if target_exe is None:
        target_exe = Path(sys.executable)
    new_exe = Path(new_exe).resolve()
    target_exe = Path(target_exe).resolve()
    img = target_exe.stem   # プロセス名(拡張子なし)

    ps = (
        "Start-Sleep -Seconds 2\r\n"
        f"Get-Process -Name '{img}' -ErrorAction SilentlyContinue | "
        "Stop-Process -Force -ErrorAction SilentlyContinue\r\n"
        f"$new = {_ps_quote(new_exe)}\r\n"
        f"$tgt = {_ps_quote(target_exe)}\r\n"
        "for ($i=0; $i -lt 60; $i++) {\r\n"
        "  try { Move-Item -LiteralPath $new -Destination $tgt -Force -ErrorAction Stop; break }\r\n"
        "  catch { Start-Sleep -Seconds 1 }\r\n"
        "}\r\n"
        "Start-Process -FilePath $tgt\r\n"
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\r\n"
    )
    helper = Path(tempfile.gettempdir()) / "gsm_selfupdate.ps1"
    helper.write_text(ps, encoding="utf-8-sig")   # BOM付きでPSのエンコード誤認を防ぐ

    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(helper)],
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True)
