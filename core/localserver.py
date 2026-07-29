"""直接(Hyper-Vなし)モード: このWindows PC上でゲームサーバーをプロセスとして動かす。

VM/SSH/systemd を使わず、GameServer(VM版)と同じメソッド面
(status/start/stop/restart/players/player_count/rcon_command/announce/restart_with_notice)
を提供する。context が deployment=="direct" のとき GameServer の代わりにこれを使う。
稼働判定=ゲームポート/RCONポートのLISTEN + pidファイルのプロセス生存。
"""
from __future__ import annotations

import re
import shlex
import socket
import subprocess
import time
from pathlib import Path

from .rcon import RconClient, RconError

CREATE_NO_WINDOW = 0x08000000     # コンソール窓を出さない


class LocalError(Exception):
    pass


class LocalGameServer:
    """GameServer と同じインターフェースを持つ、ローカルプロセス版のサーバー操作。"""

    def __init__(self, profile):
        self.profile = profile
        self._rcon_strict = (profile.game != "palworld")   # PalworldのRCONは非準拠

    # ---- 稼働判定 ----
    @staticmethod
    def _listening(port) -> bool:
        if not port:
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.6):
                return True
        except OSError:
            return False

    @property
    def _pidfile(self) -> Path:
        return Path(self.profile.directory or ".") / ".gsm_local.pid"

    def _read_pid(self):
        try:
            return int(self._pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW).stdout
            return str(pid) in out
        except Exception:          # noqa: BLE001
            return False

    def is_running(self) -> bool:
        p = self.profile
        rport = p.rcon.port if p.rcon else 0
        if self._listening(p.game_port) or self._listening(rport):
            return True
        pid = self._read_pid()
        return bool(pid and self._pid_alive(pid))

    def status(self) -> str:
        return "active" if self.is_running() else "inactive"

    # ---- 起動/停止 ----
    def start(self, progress=lambda t: None) -> None:
        if self.is_running():
            progress(f"{self.profile.display_name}: 既に起動中")
            return
        d = Path(self.profile.directory or "")
        if not d.is_dir():
            raise LocalError(f"ディレクトリがありません: {d}")
        progress(f"{self.profile.display_name}: 起動中…")
        try:
            args = shlex.split(self.profile.launch, posix=False)
        except ValueError:
            args = self.profile.launch
        creation = CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            args, cwd=str(d), creationflags=creation,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=isinstance(args, str))
        try:
            self._pidfile.write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
        gp = self.profile.game_port
        if gp:                         # 起動完了(ポートLISTEN)を待つ
            for _ in range(90):
                if self._listening(gp):
                    progress(f"{self.profile.display_name}: 起動完了(port {gp})")
                    return
                if proc.poll() is not None:
                    raise LocalError(f"{self.profile.display_name}: 起動直後に終了しました")
                time.sleep(1)
        progress(f"{self.profile.display_name}: 起動処理を実行しました")

    def stop(self, progress=lambda t: None) -> None:
        if not self.is_running():
            return
        p = self.profile
        if p.rcon and p.rcon.port and p.rcon.password and p.stop_cmd:
            try:
                progress(f"{p.display_name}: RCONで停止({p.stop_cmd})…")
                with RconClient("127.0.0.1", p.rcon.port, p.rcon.password, timeout=6) as r:
                    r.command(p.stop_cmd, strict=self._rcon_strict)
                for _ in range(40):
                    if not self.is_running():
                        progress(f"{p.display_name}: 停止しました")
                        return
                    time.sleep(1)
            except (RconError, OSError):
                pass
        pid = self._read_pid()
        if pid:
            progress(f"{p.display_name}: プロセスを強制終了…")
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
            for _ in range(10):
                if not self.is_running():
                    break
                time.sleep(1)
        if self.is_running():
            raise LocalError(f"{p.display_name}: 停止できませんでした")
        progress(f"{p.display_name}: 停止しました")

    def restart(self, progress=lambda t: None) -> None:
        self.stop(progress)
        time.sleep(2)
        self.start(progress)

    # ---- 予告付き停止/再起動(スケジューラ/GUIが呼ぶ)。ローカルは簡易実装 ----
    def restart_with_notice(self, progress=lambda t: None, cancelable: bool = False,
                            **_) -> bool:
        try:
            self.announce("[GSM] Server restarting shortly...")
        except Exception:              # noqa: BLE001
            pass
        self.restart(progress)
        return True

    def stop_with_notice(self, progress=lambda t: None, **_) -> None:
        try:
            self.announce("[GSM] Server shutting down...")
        except Exception:              # noqa: BLE001
            pass
        self.stop(progress)

    # ---- RCON / プレイヤー ----
    def rcon_command(self, cmd: str) -> str:
        p = self.profile
        if not (p.rcon and p.rcon.port and p.rcon.password):
            raise LocalError("RCONが設定されていません")
        with RconClient("127.0.0.1", p.rcon.port, p.rcon.password, timeout=6) as r:
            return r.command(cmd, strict=self._rcon_strict)

    def players(self) -> str:
        p = self.profile
        if not p.players_command:
            return ""
        try:
            return self.rcon_command(p.players_command)
        except Exception as exc:       # noqa: BLE001
            return f"RCON接続不可 ({exc})"

    def player_count(self, raw: str | None = None):
        p = self.profile
        text = raw if raw is not None else self.players()
        if not text or text.startswith("RCON接続不可"):
            return None
        if not p.players_pattern:
            return None
        m = re.search(p.players_pattern, text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return None

    def announce(self, msg: str) -> None:
        p = self.profile
        if not (p.rcon and p.rcon.port):
            return
        cmd = ("Broadcast " + msg.replace(" ", "_")) if p.game == "palworld" \
            else ("say " + msg)
        try:
            self.rcon_command(cmd)
        except Exception:              # noqa: BLE001
            pass
