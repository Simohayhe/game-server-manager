"""ホストで動く Palworld 専用サーバーの管理(ARKのPalHost版)。

Palworldは App 2394010。設定は Pal/Saved/Config/WindowsServer/PalWorldSettings.ini の
OptionSettings=(...) 1行に全部入る。ゲーム8211/UDP、RCON 25575(Source RCON)。
  - 状態  : PalServer-Win64-Shipping.exe プロセスの有無
  - 起動  : PalServer.exe(ウィンドウ非表示)
  - 停止  : RCON Save → Shutdown/DoExit、応答なければプロセス停止
RCONポート/管理パスワードは PalWorldSettings.ini から取得する。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .rcon import RconClient, RconError


@dataclass
class PalHostConfig:
    display_name: str = "Palworld"
    install_dir: str = ""        # 例 C:\PalServer
    launch_args: str = "-useperfthreads -NoAsyncLoadingThread -UseMultithreadForDS"
    process_name: str = "PalServer-Win64-Shipping"
    rcon_host: str = "127.0.0.1"

    @property
    def root(self) -> Path:
        return Path(self.install_dir)

    @property
    def exe_path(self) -> Path:
        return self.root / "PalServer.exe"

    @property
    def settings_path(self) -> Path:
        return (self.root / "Pal" / "Saved" / "Config" / "WindowsServer"
                / "PalWorldSettings.ini")

    @property
    def log_path(self) -> Path:
        return self.root / "Pal" / "Saved" / "Logs" / "Pal.log"

    def option(self, key: str, default=None):
        """PalWorldSettings.ini の OptionSettings=(...) から key の値を取り出す。"""
        p = self.settings_path
        if not p.exists():
            return default
        txt = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'OptionSettings=\((.*)\)', txt, re.DOTALL)
        body = m.group(1) if m else txt
        mm = re.search(rf'{re.escape(key)}=("([^"]*)"|[^,)\s]+)', body)
        if not mm:
            return default
        return mm.group(2) if mm.group(2) is not None else mm.group(1)

    @property
    def game_port(self) -> int:
        try:
            return int(self.option("PublicPort") or 8211)
        except ValueError:
            return 8211

    @property
    def rcon_port(self) -> int:
        try:
            return int(self.option("RCONPort") or 25575)
        except ValueError:
            return 25575

    @property
    def query_port(self) -> int:
        try:
            return int(self.option("QueryPort") or 27015)
        except ValueError:
            return 27015


class PalHostError(Exception):
    pass


class PalHost:
    """ホストPalworldサーバーのファサード。runner=ホストのPowerShell(run_ps)。"""

    def __init__(self, cfg: PalHostConfig, runner):
        self.cfg = cfg
        self.runner = runner

    def is_running(self) -> bool:
        r = self.runner.run_ps(
            f"@(Get-Process {self.cfg.process_name} -ErrorAction SilentlyContinue).Count",
            timeout=20)
        try:
            return int((r.stdout or "0").strip() or "0") > 0
        except ValueError:
            return False

    def status(self) -> str:
        return "active" if self.is_running() else "inactive"

    # ---- RCON ----
    def rcon_params(self):
        pw = self.cfg.option("AdminPassword") or ""
        return self.cfg.rcon_host, self.cfg.rcon_port, pw

    def _rcon(self) -> RconClient:
        host, port, pw = self.rcon_params()
        if not pw:
            raise PalHostError("管理パスワード(AdminPassword)が未設定です")
        return RconClient(host, port, pw)

    def rcon_command(self, cmd: str) -> str:
        with self._rcon() as r:
            return r.command(cmd, strict=False)   # PalworldのRCONは応答IDを返さない

    def players(self) -> str:
        try:
            return self.rcon_command("ShowPlayers").strip() or "(0人)"
        except (RconError, OSError, PalHostError) as e:
            return f"RCON接続不可 ({e})"

    def num_players(self) -> int:
        """ShowPlayers の行数(ヘッダ除く)で人数を数える。"""
        try:
            raw = self.rcon_command("ShowPlayers")
        except Exception:
            return 0
        lines = [l for l in (raw or "").splitlines() if l.strip()]
        # 先頭行は "name,playeruid,steamid" ヘッダ
        return max(0, len(lines) - 1)

    def info(self) -> dict:
        return {
            "name": self.cfg.option("ServerName") or self.cfg.display_name,
            "running": self.is_running(),
            "players": self.players() if self.is_running() else "(停止中)",
        }

    # ---- 起動 / 停止 ----
    def start(self, progress=lambda t: None) -> None:
        if self.is_running():
            raise PalHostError("既にPalworldサーバーが起動しています(二重起動防止)")
        if not self.cfg.exe_path.exists():
            raise PalHostError(f"PalServer.exe が見つかりません: {self.cfg.exe_path}")
        wd = str(self.cfg.root)
        progress("Palworldサーバーを起動中…(コンソール非表示)")
        r = self.runner.run_ps(
            f"Start-Process -FilePath '{self.cfg.exe_path}' -WorkingDirectory '{wd}' "
            f"-WindowStyle Hidden -ArgumentList '{self.cfg.launch_args}'; 'STARTED'",
            timeout=60)
        if "STARTED" not in (r.stdout or ""):
            raise PalHostError(f"起動に失敗しました: {r.stderr.strip()}")

    def stop(self, progress=lambda t: None) -> None:
        if not self.is_running():
            return
        try:
            progress("ワールド保存中(Save)…")
            self.rcon_command("Save")
            time.sleep(2)
            progress("サーバー終了指示(Shutdown)…")
            self.rcon_command("Shutdown 1 ServerStopping")
        except Exception:
            pass
        for _ in range(20):                  # 最大60秒、消滅を待つ
            if not self.is_running():
                progress("停止しました(保存済み)")
                return
            time.sleep(3)
        progress("応答が無いためプロセスを停止…")
        self.runner.run_ps(
            f"Get-Process {self.cfg.process_name} -ErrorAction SilentlyContinue "
            f"| Stop-Process -Force", timeout=30)

    def restart(self, progress=lambda t: None) -> None:
        self.stop(progress=progress)
        time.sleep(3)
        self.start(progress=progress)

    def tail_log(self, lines: int = 400) -> str:
        p = self.cfg.log_path
        if not p.exists():
            return f"(ログがまだありません: {p})"
        try:
            data = p.read_bytes()
        except OSError as e:
            return f"(ログ読み取り不可: {e})"
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])
